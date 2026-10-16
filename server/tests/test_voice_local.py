"""Local voice on the call line (owner's decision 2026-10-16): the voice model on the PC answers on
/ws/voice; Claude only through ask_kairo; the old pipeline when the voice model isn't there.
The voice model is tests/fakes/fake_llama.py behind the real manager; Claude is the fake CLI."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import realtime
from app.brain.voice_local import VoiceModel
from app.db import make_engine
from app.main import create_app
from tests.test_voice_llm import FAKE, free_port
from tests.test_voicelink import PCM, FakeVoice, frames_until, utter


@pytest.fixture
def local(migrated, tmp_path, monkeypatch):
    record = tmp_path / "llama.jsonl"
    monkeypatch.setenv("FAKE_LLAMA_RECORD", str(record))
    app = create_app(migrated)
    app.state.voice = FakeVoice()
    model = tmp_path / "lfm.gguf"
    model.write_bytes(b"GGUF")
    vm = VoiceModel(lambda: ([sys.executable, str(FAKE)], model, "lfm2.5-2.6b", 4096), log_dir=tmp_path / "logs",
                    port=free_port())
    assert vm.wait_ready(20)
    app.state.voice_llm = vm
    with TestClient(app) as c:
        c.voice, c.vm, c.record, c.settings = app.state.voice, vm, record, migrated
        yield c
    vm.shutdown()


def requests_seen(c) -> list[dict]:
    return [json.loads(x) for x in c.record.read_text(encoding="utf-8").splitlines()] if c.record.is_file() else []


def test_small_talk_is_answered_by_the_voice_model_not_claude(local):
    local.voice.text = "hey how are you"
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws)
    done = next(f for f in fr if isinstance(f, dict) and f["type"] == "done")
    assert done["model"] == "lfm2.5-2.6b" and done["text"] == "Sure, happy to help with that."
    assert done["server"]["llm_ms"] is not None and done["server"]["stt_ms"] >= 0
    assert local.voice.spoken and local.voice.spoken[0][0].startswith("Sure")
    msgs = [(m["role"], m["text"]) for m in local.get("/api/chat/messages").json()["messages"]]
    assert msgs == [("owner", "(spoken, on a call) hey how are you"), ("kairo", "Sure, happy to help with that.")]
    reqs = requests_seen(local)
    assert [t["function"]["name"] for t in reqs[-1]["tools"]] == ["ask_kairo"]
    assert reqs[-1]["chat_template_kwargs"] == {"enable_thinking": False}


def test_a_data_question_goes_to_claude_through_ask_kairo(local):
    local.voice.text = "Remind me to call the dentist tomorrow"
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws, limit=400)
    deltas = "".join(f["text"] for f in fr if isinstance(f, dict) and f["type"] == "delta")
    done = next(f for f in fr if isinstance(f, dict) and f["type"] == "done")
    assert deltas.startswith("Sec, checking.")                      # the filler first, never silence
    assert "echo: (spoken, on a call) Remind me to call the dentist tomorrow" in done["text"]   # Claude's answer, said
    assert done["server"]["tool_ms"] is not None
    tool_round = requests_seen(local)[-1]
    assert [m["role"] for m in tool_round["messages"]][-2:] == ["assistant", "tool"]
    msgs = [m["role"] for m in local.get("/api/chat/messages").json()["messages"]]
    assert msgs[0] == "owner" and msgs[-1] == "kairo"


def test_the_old_pipeline_answers_when_the_voice_model_is_not_there(local):
    local.vm.pause()
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws)
    done = next(f for f in fr if isinstance(f, dict) and f["type"] == "done")
    assert done["model"] == "haiku" and done["text"].startswith("echo:")   # Claude talked, as before


def test_openai_as_the_engine_keeps_the_voice_model_out_of_the_old_line(local):
    with Session(make_engine(local.settings.db_url)) as s:
        realtime.save_settings(s, {"engine": "openai"})
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws)
    assert next(f for f in fr if isinstance(f, dict) and f["type"] == "done")["model"] == "haiku"


def test_the_voice_settings_say_local_by_default(local):
    st = local.get("/api/voice/realtime").json()
    assert st["available"] is False and st["reason"] == "local" and st["settings"]["engine"] == "local"
    lab = local.get("/api/voice/lab").json()
    assert lab["engine"] == "local" and lab["voice_model"]["state"] == "ready" and lab["state"] == {"running": False}


def test_the_call_prompt_is_read_once_before_the_first_words(local):
    with local.websocket_connect("/ws/voice") as ws:
        time.sleep(0.5)                                             # the line is open, the owner hasn't spoken yet
        utter(ws)
        frames_until(ws)
    reqs = requests_seen(local)
    assert reqs[0]["max_tokens"] == 1                                # the priming read of the call's prompt
    assert reqs[0]["messages"][0] == reqs[1]["messages"][0]          # same start: the cache holds


# ---------------------------------------------------------------- smart endpointing (owner's request 2026-10-16)

class Turny:
    """A turn detector that says what the test wants."""
    pad_side, failed, last_ms = "right", None, 2

    def __init__(self, p):
        self.p, self.calls = p, 0

    def available(self):
        return True

    def probability(self, pcm):
        self.calls += 1
        return self.p


@pytest.fixture
def smart(migrated, request):
    p = request.node.get_closest_marker("turn_p").args[0] if request.node.get_closest_marker("turn_p") else 0.9
    app = create_app(migrated)
    app.state.voice = FakeVoice()
    app.state.smart_turn = Turny(p)
    with TestClient(app) as c:
        c.turny, c.voice = app.state.smart_turn, app.state.voice
        yield c


def say_and_pause(ws, pcm=PCM):
    ws.send_json({"type": "start", "rate": 16000, "path": "call"})
    ws.send_bytes(pcm)
    ws.send_json({"type": "pause"})


def test_the_pc_ends_a_finished_turn_at_the_first_short_pause(smart):
    with smart.websocket_connect("/ws/voice") as ws:
        hello = ws.receive_json()
        assert hello == {"type": "hello", "endpoint": "smart", "pause_ms": 200, "end_ms": 1500}
        t0 = time.monotonic()
        say_and_pause(ws)
        ep = ws.receive_json()
        assert ep["type"] == "endpoint" and ep["turn"] == 1 and ep["p"] == 0.9 and time.monotonic() - t0 < 0.3
        fr = frames_until(ws)
        ws.send_json({"type": "end", "path": "call", "turn": 1})       # the phone's own late end: ignored quietly
        assert fr[0]["type"] == "transcript" and any(f.get("type") == "done" for f in fr if isinstance(f, dict))
    assert smart.voice.heard and len(smart.voice.heard) == 1            # transcribed once, at the pause


@pytest.mark.turn_p(0.05)
def test_a_turn_that_isnt_finished_waits_for_more(smart):
    with smart.websocket_connect("/ws/voice") as ws:
        ws.receive_json()                                               # hello
        say_and_pause(ws)
        time.sleep(0.3)
        ws.send_bytes(PCM)                                              # they talked on: that pause is stale
        ws.send_json({"type": "pause"})
        t0 = time.monotonic()
        ep = ws.receive_json()
        assert ep["type"] == "endpoint" and 0.9 < time.monotonic() - t0 < 2.0   # after a longer quiet, not at once
        assert ep["turn"] == 1                                          # one turn, not two
        frames_until(ws)


def test_without_the_turn_detector_the_phone_decides_as_before(local):
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)                                                       # no hello: start, audio, end
        fr = frames_until(ws)
    assert fr[0]["type"] == "transcript"


# ---------------------------------------------------------------- the Today card and the wait (owner's request 2026-10-16)

def card(c, **sections):
    from app import todaycard
    reads = {"n": 0}

    def tasks():
        reads["n"] += 1
        return f"- task read {reads['n']}"
    srcs = {k: (lambda v=v: v) for k, v in sections.items()}
    c.app.state.today_card = todaycard.Keeper({**srcs, "tasks": tasks})
    c.app.state.today_card.refresh()
    return reads


def test_a_card_question_is_answered_on_the_pc_without_claude(local):
    from app import voicestats
    card(local, weather="Home: now 12°C, cloudy")
    local.voice.text = "What's the weather like?"
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws)
    done = next(f for f in fr if isinstance(f, dict) and f["type"] == "done")
    assert done["text"] == "From your card: Home: now 12°C, cloudy" and done["server"]["tool_ms"] is None
    msgs = [m["role"] for m in local.get("/api/chat/messages").json()["messages"]]
    assert msgs == ["owner", "kairo"]                                          # Claude never ran
    with Session(make_engine(local.settings.db_url)) as s:
        assert voicestats.summary(s)["card"] == 1
    assert local.get("/api/voice/lab").json()["live"]["on_pc_pct"] == 100


def test_after_claude_did_something_the_card_is_read_again(local):
    reads = card(local, weather="Home: now 12°C")
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)                                                              # "Remind me to call the dentist tomorrow"
        frames_until(ws, limit=400)
    for _ in range(50):
        if reads["n"] >= 2:
            break
        time.sleep(0.05)
    assert reads["n"] >= 2                                                     # tasks read again right after Claude


def seed_urgent(c):
    import datetime as dt
    from app import bringup
    from app.db import OpenLoop
    from tests.test_pa import PROFILE
    now = dt.datetime.now(PROFILE.tz)
    with Session(make_engine(c.settings.db_url)) as s:
        lp = OpenLoop(kind="task", source="chat", what="sign the school form", urgency="urgent",
                      check_json=json.dumps({"verdict": "keep"}), checked_at=now.astimezone(dt.timezone.utc))
        s.add(lp)
        s.commit()
        item = bringup.Item(f"L{lp.id}", "loop", "task: sign the school form", "urgent", "The school form has to be signed today.",
                            {"en": "The school form has to be signed today.", "de": "Das Formular muss heute unterschrieben sein."})
        bringup.store(s, [item], "test", now)
    return item


def test_an_urgent_item_comes_in_the_wait_before_claudes_answer(local):
    seed_urgent(local)
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        fr = frames_until(ws, limit=400)
    deltas = "".join(f["text"] for f in fr if isinstance(f, dict) and f["type"] == "delta")
    assert deltas.startswith("Sec, checking. Before I forget, this one's urgent: The school form has to be signed today.")
    assert deltas.index("urgent") < deltas.index("echo:")                    # first, before the answer
    done = next(f for f in fr if isinstance(f, dict) and f["type"] == "done")
    assert "[[" not in done["text"]


def test_what_the_owner_says_to_it_goes_to_claude_during_the_call(local):
    asked = []

    async def ask(request, lines):
        asked.append((request, lines))
        return "Noted.", False
    local.app.state.report_ask = ask
    seed_urgent(local)
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        frames_until(ws, limit=400)
        local.voice.text = "Yes I signed it this morning"
        utter(ws, turn=2)
        frames_until(ws, limit=400)
        for _ in range(100):                                                   # owner's decision 2026-10-16: live
            if asked:
                break
            time.sleep(0.05)
        request, lines = asked[0]
        assert request.startswith("[On this call (record it now, say nothing back)")
        assert '"Yes I signed it this morning"' in request and "sign the school form" in request
        assert "Act only on the owner's words" in request and lines == ["Owner: Yes I signed it this morning"]
    time.sleep(0.3)
    assert len(asked) == 1                                                     # not again after the hang-up
