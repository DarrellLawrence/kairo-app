"""G13 end to end: a voice turn that Kairo cannot confirm as the owner reaches Claude, and Claude's changes are refused.

The real PreToolUse hook runs (the fake Claude starts it like the CLI does), the voice model is the fake llama, the speaker
model is tests/test_speaker.py's pitch model. Over the old line (/ws/voice) here; over WebRTC (it needs the call packages,
so those two tests live in test_speaker.py, which CI's voice jobs run) there. Written before the wiring in main.py."""

from __future__ import annotations

import json
import time

import pytest

np = pytest.importorskip("numpy")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import flags  # noqa: E402
from app import speaker_label as sl  # noqa: E402
from app.db import AuditLog, SettingKV, Task, make_engine  # noqa: E402
from tests.test_speaker import NEAR_HZ, OTHER_HZ, OWNER_HZ, PitchModel, pcm, voice  # noqa: E402
from tests.test_chat_ws import converse  # noqa: E402
from tests.test_voice_local import local  # noqa: E402,F401  (the app with the fake voice model)
from tests.test_voicelink import frames_until, utter  # noqa: E402

FLAG = "g13.speaker_label_gate"
WRITE = 'Remind me to call the dentist tomorrow [[fake-tool tasks_create {"title": "call the dentist"}]]'
READ = 'Remind me what is on my list [[fake-tool tasks_list {}]]'


def tasks(c) -> list[str]:
    with Session(make_engine(c.settings.db_url)) as s:
        return [t.title for t in s.exec(select(Task))]


def set_flag(c, on):
    with Session(make_engine(c.settings.db_url)) as s:
        flags.set_flag(s, FLAG, on)


@pytest.fixture
def labels(monkeypatch):
    """Every label the brain runner was asked to write, in order (runs that are not voice turns ask with None: left out)."""
    seen: list[str] = []
    real = sl.VoiceTurns.begin

    def spy(self, label, **kw):
        if label is not None:
            seen.append(label)
        return real(self, label, **kw)
    monkeypatch.setattr(sl.VoiceTurns, "begin", spy)
    return seen


def teach(c):
    c.app.state.speaker.model = PitchModel()
    c.app.state.speaker.enroll(pcm(voice(30, seed=1)))


def say(c, text, audio, path="call") -> str:
    """One spoken turn on the old line; returns what Kairo said back."""
    c.voice.text = text
    with c.websocket_connect("/ws/voice") as ws:
        utter(ws, pcm(audio), path=path, turn=1)
        fr = frames_until(ws, limit=600)
    done = next((f for f in fr if isinstance(f, dict) and f["type"] == "done"), None)
    return (done or {}).get("text", "") if done else " ".join(f.get("text", "") for f in fr if isinstance(f, dict))


# ---------------------------------------------------------------- the backend's own check (calls arriving through mcp_kairo)

def test_the_backend_refuses_a_change_on_an_unconfirmed_voice_turn_and_says_so_in_the_activity_log(local, monkeypatch):
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "LOCAL_HOSTS", {"testclient", "127.0.0.1"})
    c = local
    with Session(make_engine(c.settings.db_url)) as s:
        s.add(SettingKV(key=sl.KEY, value=json.dumps({"label": "unsure", "run": "r1", "at": "2026-10-24T10:00:00+00:00"})))
        s.commit()
    c.app.state.runner.current_origin = "owner_chat"
    h = {"authorization": f"Bearer {c.app.state.tool_token}"}

    def call(name, args):
        return c.post("/internal/tools/call", json={"name": name, "arguments": args}, headers=h).json()

    refused = call("tasks_create", {"title": "x"})
    assert refused["is_error"] and "voice" in refused["text"].lower()
    assert call("propose_action", {"action": "gmail.label", "args": {"message_id": "a", "add": ["Kairo/Reply"]}})["is_error"]
    assert not call("tasks_list", {})["is_error"]                    # reading is fine
    assert tasks(c) == []
    with Session(make_engine(c.settings.db_url)) as s:
        rows = [r.outcome for r in s.exec(select(AuditLog).where(AuditLog.actor == "brain"))]
    assert any("denied" in r and "voice turn: unsure" in r for r in rows), rows
    with Session(make_engine(c.settings.db_url)) as s:
        flags.set_flag(s, FLAG, False)
    assert not call("tasks_create", {"title": "x"})["is_error"] and tasks(c) == ["x"]       # off: what it was


# ---------------------------------------------------------------- the old line (/ws/voice)

def test_an_owner_turn_changes_things_a_short_weak_one_does_not(local, labels):
    teach(local)
    owner = say(local, WRITE, voice(2.0, OWNER_HZ, seed=2))
    assert "TOOL-BLOCKED" not in owner and tasks(local) == []         # allowed: the hook let it through (the tool server is not up here)
    unsure = say(local, WRITE, voice(0.6, NEAR_HZ, seed=5))
    assert "TOOL-BLOCKED tasks_create" in unsure and "Kairo gate" in unsure
    assert "unsure" not in unsure and "stranger" not in unsure        # the model is never told which
    assert labels == ["owner", "unsure"]


def test_the_unconfirmed_turn_may_still_read(local):
    teach(local)
    said = say(local, READ, voice(0.6, NEAR_HZ, seed=5))
    assert "TOOL-BLOCKED" not in said


def test_someone_else_is_not_answered_at_all_as_before(local, labels):
    teach(local)
    c = local
    c.voice.text = WRITE
    with c.websocket_connect("/ws/voice") as ws:
        utter(ws, pcm(voice(2.0, OTHER_HZ, seed=3)), turn=1)
        f = ws.receive_json()
    assert f["type"] == "ignored" and f["reason"] == "not_you"
    assert labels == [] and tasks(c) == []


def test_before_the_voice_is_taught_a_call_is_unchecked_and_works_as_before(local, labels):
    local.app.state.speaker.model = PitchModel()                      # no voiceprint
    said = say(local, WRITE, voice(0.6, NEAR_HZ, seed=5))
    assert "TOOL-BLOCKED" not in said and labels == ["unchecked"]


def test_the_voice_button_is_the_owners_own_press(local, labels):
    teach(local)
    said = say(local, WRITE, voice(0.6, OTHER_HZ, seed=5), path="chat_voice")
    assert "TOOL-BLOCKED" not in said and labels == ["unchecked"]


def test_the_flag_off_is_what_it_was(local, labels):
    teach(local)
    set_flag(local, False)
    said = say(local, WRITE, voice(0.6, NEAR_HZ, seed=5))
    assert "TOOL-BLOCKED" not in said
    assert labels == ["unsure"]                                       # asked for, but nothing was written
    set_flag(local, None)
    assert "TOOL-BLOCKED tasks_create" in say(local, WRITE, voice(0.6, NEAR_HZ, seed=6))


def test_a_typed_message_after_a_voice_turn_is_not_blocked(local):
    teach(local)
    assert "TOOL-BLOCKED" in say(local, WRITE, voice(0.6, NEAR_HZ, seed=5))
    with Session(make_engine(local.settings.db_url)) as s:
        assert s.get(SettingKV, sl.KEY) is None                       # removed when the run ended
    with local.websocket_connect("/ws/chat") as ws:
        out = converse(ws, 'Remind me [[fake-tool tasks_create {"title": "typed"}]]')[-1]["text"]
    assert "TOOL-BLOCKED" not in out and "TOOL-ERROR tasks_create" in out      # the hook let it through (no tool server here)


def test_the_words_of_an_earlier_unsure_turn_do_not_become_the_owners(local, labels):
    """The owner talked over an answer: the earlier words are carried into the next turn. The next turn is no more
    trusted than the least trusted words in it."""
    teach(local)
    c = local
    c.voice.text = WRITE
    with c.websocket_connect("/ws/voice") as ws:
        # turn 2 arrives first as "stopped" turn 1: send the short weak turn as turn 1, then stop it, then the owner as turn 2
        ws.send_json({"type": "stop", "turn": 1})
        utter(ws, pcm(voice(0.6, NEAR_HZ, seed=5)), turn=1)           # talked over: carried, not answered
        utter(ws, pcm(voice(2.0, OWNER_HZ, seed=2)), turn=2)
        fr = frames_until(ws, limit=600)
    done = next(f for f in fr if isinstance(f, dict) and f["type"] == "done")
    assert "TOOL-BLOCKED" in done["text"] and labels[-1:] == ["unsure"]


# ---------------------------------------------------------------- an answer to a raised item is not lost silently (review finding)

def _raise_then_reply(c, first, reply_audio, *, third=None, push_log=None):
    """The urgent item is raised in turn 1's wait; the reply is turn 2 (and, when given, turn 3 says it again).
    Returns what report_ask was given, in order."""
    from tests.test_voice_local import seed_urgent
    real = c.app.state.report_ask
    seen: list[tuple[str, str | None]] = []

    async def ask(request, lines):
        seen.append((request, sl.current()))
        return await real(request, lines)
    c.app.state.report_ask = ask
    seed_urgent(c)
    c.voice.text = "Remind me what's on my list"
    with c.websocket_connect("/ws/voice") as ws:
        utter(ws, pcm(first), turn=1)
        frames_until(ws, limit=400)
        c.voice.text = "Yes I signed it this morning"
        utter(ws, pcm(reply_audio), turn=2)
        frames_until(ws, limit=400)
        if third is not None:
            c.voice.text = "Yes I signed the school form this morning"
            utter(ws, pcm(third), turn=3)
            frames_until(ws, limit=400)
        time.sleep(0.6)                                               # the live report runs beside the call
    time.sleep(0.5)                                                   # ... and the one after the hang-up
    return seen


@pytest.fixture
def pushes(local, monkeypatch):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(local.app.state.pusher, "notify", lambda title, body, url="/", **k: sent.append((title, body)) or "sent")
    return sent


def test_a_confirmed_reply_to_a_raised_item_is_recorded_as_before(local, pushes):
    teach(local)
    seen = _raise_then_reply(local, voice(2.0, OWNER_HZ, seed=2), voice(2.0, OWNER_HZ, seed=3))
    assert len(seen) == 1 and seen[0][1] == "owner"                    # recorded, under the owner's label
    assert not [p for p in pushes if "confirm" in p[1]]


def test_a_short_weak_reply_to_a_raised_item_is_not_recorded_and_the_owner_is_told(local, pushes):
    """The reviewer's case: 0.6 s of 'yes' scores weakly. The report would be refused by the hook and, being silent,
    never noticed. Now nothing is sent, and the owner gets a push saying to say it again or mark it in the app."""
    teach(local)
    seen = _raise_then_reply(local, voice(2.0, OWNER_HZ, seed=2), voice(0.6, NEAR_HZ, seed=5))
    assert seen == []
    told = [p for p in pushes if "couldn't confirm" in p[1]]
    assert len(told) == 1 and "sign the school form" in told[0][1] and "app" in told[0][1]
    assert "unsure" not in told[0][1] and "stranger" not in told[0][1]       # never which label


def test_saying_it_again_clearly_records_it(local, pushes):
    teach(local)
    seen = _raise_then_reply(local, voice(2.0, OWNER_HZ, seed=2), voice(0.6, NEAR_HZ, seed=5), third=voice(2.0, OWNER_HZ, seed=4))
    assert len(seen) == 1 and "Yes I signed the school form this morning" in seen[0][0] and seen[0][1] == "owner"
    assert len([p for p in pushes if "couldn't confirm" in p[1]]) == 1


def test_a_weak_turn_elsewhere_on_the_call_does_not_stop_a_confirmed_reply(local, pushes):
    """The item is raised in a weak turn; the reply is confirmed: it is recorded (labels are per reply, not per call)."""
    teach(local)
    seen = _raise_then_reply(local, voice(0.6, NEAR_HZ, seed=5), voice(2.0, OWNER_HZ, seed=3))
    assert len(seen) == 1 and seen[0][1] == "owner" and not pushes_about(pushes)


def pushes_about(pushes):
    return [p for p in pushes if "couldn't confirm" in p[1]]


def test_with_the_switch_off_a_weak_reply_is_recorded_as_before(local, pushes):
    teach(local)
    set_flag(local, False)
    seen = _raise_then_reply(local, voice(2.0, OWNER_HZ, seed=2), voice(0.6, NEAR_HZ, seed=5))
    assert len(seen) == 1 and not pushes_about(pushes)


def test_before_the_voice_is_taught_a_reply_is_recorded_as_before(local, pushes):
    local.app.state.speaker.model = PitchModel()                      # no voiceprint: unchecked
    seen = _raise_then_reply(local, voice(2.0, OWNER_HZ, seed=2), voice(0.6, NEAR_HZ, seed=5))
    assert len(seen) == 1 and seen[0][1] == "unchecked" and not pushes_about(pushes)


def test_after_the_hang_up_a_reply_keeps_its_own_label(local, pushes, monkeypatch):
    """Replies not sent during the call go after it. Each goes under the label it was said with: a confirmed one is
    recorded even if the call had weak turns; a weak one is not, and the owner is told."""
    from app import voicemouth
    real_init = voicemouth.Mouth.__init__

    def no_live_report(self, *a, **k):
        k["report"] = None                                            # collect everything for after the call
        real_init(self, *a, **k)
    monkeypatch.setattr(voicemouth.Mouth, "__init__", no_live_report)
    teach(local)
    owner = _raise_then_reply(local, voice(0.6, NEAR_HZ, seed=5), voice(2.0, OWNER_HZ, seed=3))
    assert len(owner) == 1 and owner[0][1] == "owner" and not pushes_about(pushes)
    weak = _raise_then_reply(local, voice(2.0, OWNER_HZ, seed=2), voice(0.6, NEAR_HZ, seed=5))
    assert weak == [] and len(pushes_about(pushes)) == 1
