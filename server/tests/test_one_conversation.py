"""One conversation with Kairo, forever (owner's request 2026-10-14). Written before the code.

No threads, no "new chat": chat, calls and voice notes land in the same thread. Older messages
are folded into a conversation memory that is part of every prompt; the CLI session starts
fresh once the memory has caught up (so it never grows without end). Kairo never talks about
"this session"."""

from __future__ import annotations

import datetime as dt
import json

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import chat, memory
from app.chat import AskResult
from app.db import Approval, ChatMessage, ChatThread, SettingKV, make_engine
from app.main import create_app
from app.settings import REPO_ROOT
from tests.test_chat_ws import converse


def seed_threads(engine) -> list[int]:
    """What kairo-home has: one thread per day, a card that points to an older one."""
    with Session(engine) as s:
        ids = []
        for day in (1, 2, 3):
            t = ChatThread(brain="claude_cli", title=f"2026-10-0{day}",
                           started_at=dt.datetime(2026, 10, day, 9, tzinfo=dt.timezone.utc), brain_session_id=f"sid{day}")
            s.add(t)
            s.commit()
            s.refresh(t)
            ids.append(t.id)
            for role, text in (("owner", f"day {day} question"), ("kairo", f"day {day} answer")):
                s.add(ChatMessage(thread_id=t.id, role=role, text=text))
                s.commit()
        s.add(Approval(tool="gmail.send", args_json="{}", summary="x", tier=3, status="done", thread_id=ids[1]))
        s.commit()
        return ids


def test_old_threads_become_one_conversation(migrated):
    engine = make_engine(migrated.db_url)
    seed_threads(engine)
    with TestClient(create_app(migrated)) as c:
        with Session(engine) as s:
            threads = s.exec(select(ChatThread)).all()
            assert len(threads) == 1 and threads[0].brain_session_id is None
            tid = threads[0].id
            assert {m.thread_id for m in s.exec(select(ChatMessage)).all()} == {tid}
            assert s.exec(select(Approval)).one().thread_id == tid
        r = c.get("/api/chat/messages").json()
        assert [m["text"] for m in r["messages"]] == [f"day {d} {k}" for d in (1, 2, 3) for k in ("question", "answer")]
        assert r["has_more"] is False
        assert c.post("/api/threads").status_code in (404, 405)            # no "new chat" any more
        assert c.get("/api/threads").status_code in (404, 405)


def test_chat_on_different_days_stays_in_one_thread(migrated, monkeypatch):
    with TestClient(create_app(migrated)) as c, c.websocket_connect("/ws/chat") as ws:
        a = converse(ws, "first")[0]["id"]
        monkeypatch.setattr(chat, "utcnow", lambda: dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3))
        b = converse(ws, "three days later", thread_id=12345)[0]["id"]   # an old client's thread id is ignored
        assert a == b


def test_messages_page_backwards(migrated):
    engine = make_engine(migrated.db_url)
    with TestClient(create_app(migrated)) as c:
        with Session(engine) as s:
            tid = chat.main_thread(s, brain_name="claude_cli").id
            for i in range(25):
                chat.add_message(s, tid, "owner", f"m{i}")
        last = c.get("/api/chat/messages?limit=10").json()
        assert [m["text"] for m in last["messages"]] == [f"m{i}" for i in range(15, 25)] and last["has_more"] is True
        before = last["messages"][0]["id"]
        older = c.get(f"/api/chat/messages?limit=10&before={before}").json()
        assert [m["text"] for m in older["messages"]] == [f"m{i}" for i in range(5, 15)]


# ---------------------------------------------------------------- memory: nothing gets lost

def fill(engine, n: int) -> int:
    with Session(engine) as s:
        tid = chat.main_thread(s, brain_name="claude_cli").id
        for i in range(n):
            chat.add_message(s, tid, "owner" if i % 2 == 0 else "kairo", f"message {i}: the dentist is on Thursday" if i == 3 else f"message {i}")
        return tid


def test_older_messages_are_folded_into_memory(migrated):
    engine = make_engine(migrated.db_url)
    fill(engine, memory.KEEP + memory.FOLD_AFTER + 5)
    prompts = []

    def ask(prompt, system, purpose=None, **kw):
        prompts.append((purpose, prompt))
        return AskResult("- The dentist appointment is on Thursday.\n- Prefers short answers.")

    with Session(engine) as s:
        assert memory.fold(s, ask) is True
        m = memory.get(s)
        assert "dentist" in m["text"]
        newest = s.exec(select(ChatMessage).order_by(ChatMessage.id.desc())).all()
        assert m["upto"] == newest[memory.KEEP].id                      # the newest KEEP stay verbatim
        assert "message 3: the dentist is on Thursday" in prompts[0][1] and prompts[0][0] == "summary"
        assert s.get(SettingKV, memory.FRESH_KEY).value == "1"           # next turn: a new CLI session
        assert memory.fold(s, ask) is False                             # nothing new to fold


def test_too_little_to_fold_does_nothing(migrated):
    engine = make_engine(migrated.db_url)
    fill(engine, memory.KEEP + 3)
    with Session(engine) as s:
        assert memory.fold(s, lambda *a, **k: AskResult("x")) is False and memory.get(s)["text"] == ""


def test_memory_and_recent_messages_reach_the_model_and_the_session_rotates(migrated, tmp_path, monkeypatch):
    rec = tmp_path / "rec.json"
    monkeypatch.setenv("KAIRO_HOOK_FAKE_RECORD", str(rec))
    engine = make_engine(migrated.db_url)
    with TestClient(create_app(migrated)) as c, c.websocket_connect("/ws/chat") as ws:
        converse(ws, "hello")
        converse(ws, "and again")
        assert "--resume" in json.loads(rec.read_text())["argv"]
        with Session(engine) as s:
            memory.save(s, "- The dentist appointment is on Thursday.", upto=1)
            memory.set_fresh(s)
        converse(ws, "when is the dentist?")
        run = json.loads(rec.read_text())
        assert "--resume" not in run["argv"]                             # rotated: fresh session ...
        assert "# Conversation memory" in run["system"] and "dentist appointment is on Thursday" in run["system"]
        assert "# Recent conversation" in run["system"] and "and again" in run["system"]   # ... with the recent messages
        assert "when is the dentist?" not in run["system"]               # the new message goes as the message itself
        converse(ws, "thanks")
        run = json.loads(rec.read_text())
        assert "--resume" in run["argv"] and "# Conversation memory" in run["system"]
        assert "# Recent conversation" not in run["system"]              # the resumed session has them already


def test_voice_notes_and_calls_land_in_the_one_conversation(migrated):
    with TestClient(create_app(migrated)) as c, c.websocket_connect("/ws/chat") as ws:
        a = converse(ws, "typed")[0]["id"]
        b = converse(ws, "(spoken, on a call) said on a call")[0]["id"]
        assert a == b
    from app import voicenote
    assert "main_thread" in open(voicenote.__file__, encoding="utf-8").read()


def test_personality_knows_there_is_one_conversation():
    text = (REPO_ROOT / "config" / "personality.md").read_text(encoding="utf-8")
    assert "one conversation" in text.lower() and "this session" in text
