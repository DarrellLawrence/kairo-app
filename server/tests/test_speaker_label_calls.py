"""G13 end to end: a voice turn that Kairo cannot confirm as the owner reaches Claude, and Claude's changes are refused.

The real PreToolUse hook runs (the fake Claude starts it like the CLI does), the voice model is the fake llama, the speaker
model is tests/test_speaker.py's pitch model. Over the old line (/ws/voice) and, when the call packages are installed,
over WebRTC. Written before the wiring in main.py."""

from __future__ import annotations

import asyncio
import json

import pytest

np = pytest.importorskip("numpy")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import flags  # noqa: E402
from app import speaker as sp  # noqa: E402
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

    def spy(self, label):
        if label is not None:
            seen.append(label)
        return real(self, label)
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


# ---------------------------------------------------------------- over WebRTC (needs the call packages)

def test_over_webrtc_an_unconfirmed_turn_cannot_change_things(local, monkeypatch, labels):
    pytest.importorskip("pipecat")
    pytest.importorskip("aiortc")
    from app import voicertc
    from app.voicertc import OpenerCache
    from tests import test_voice_rtc as rtc
    monkeypatch.setattr(voicertc, "ALLOW_LOOPBACK", True)
    local.app.state.openers = OpenerCache(lambda text, lang: (b"\x00\x10" * 2400, 24000))
    local.voice.text = WRITE
    local.app.state.voice = rtc.ToneVoice(local.voice)

    class Const(PitchModel):
        def embed(self, samples):
            return np.array([1.0, 0.0], dtype=np.float32)             # whoever talks has the owner's print
    local.app.state.speaker.model = Const()
    local.app.state.speaker.enroll(pcm(voice(30, seed=1)))
    # the check is on, and too slow for this turn: still answered, but nobody could say it was the owner
    monkeypatch.setattr(sp, "WAIT_MAX_S", 0.05)
    real_measure = local.app.state.speaker.measure

    def slow(pcm_, on):
        import time
        time.sleep(0.5)
        return real_measure(pcm_, on)
    local.app.state.speaker.measure = slow
    phone = asyncio.run(rtc._app_call(local, seconds=25))
    done = [e for e in phone.events if e.get("type") == "done"]
    assert done and "TOOL-BLOCKED tasks_create" in done[0]["text"], phone.events
    assert labels == ["unsure"] and tasks(local) == []


def test_over_webrtc_an_owner_turn_is_not_blocked(local, monkeypatch, labels):
    pytest.importorskip("pipecat")
    pytest.importorskip("aiortc")
    from app import voicertc
    from app.voicertc import OpenerCache
    from tests import test_voice_rtc as rtc
    monkeypatch.setattr(voicertc, "ALLOW_LOOPBACK", True)
    local.app.state.openers = OpenerCache(lambda text, lang: (b"\x00\x10" * 2400, 24000))
    local.voice.text = WRITE
    local.app.state.voice = rtc.ToneVoice(local.voice)

    class Const(PitchModel):
        def embed(self, samples):
            return np.array([1.0, 0.0], dtype=np.float32)
    local.app.state.speaker.model = Const()
    local.app.state.speaker.enroll(pcm(voice(30, seed=1)))
    phone = asyncio.run(rtc._app_call(local, seconds=25))
    done = [e for e in phone.events if e.get("type") == "done"]
    assert done and "TOOL-BLOCKED" not in done[0]["text"], phone.events
    assert labels == ["owner"]
