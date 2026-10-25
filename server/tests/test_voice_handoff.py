"""G5 hand-off at the app level (voice-recipe section 8): what ask_kairo gets from Claude while Claude is still
working. The brain is the fake Claude CLI (tests/fakes/fake_claude.py), which ends an answer with message_stop and then,
like the real one, takes FAKE_CLAUDE_TAIL_S before it sends the result. The voice model's side is in test_voicemouth.py."""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import chat, conversation, voicertc
from app.db import SettingKV, make_engine
from app.main import create_app
from tests.test_voice_local import local  # noqa: F401  (the app with the fake voice model and Claude)
from tests.test_voice_rtc import _app_call, _needs_pipecat

TAIL_S = 1.2


@pytest.fixture
def tail(monkeypatch):
    monkeypatch.setenv("KAIRO_HOOK_FAKE_TAIL_S", str(TAIL_S))        # the CLI's summary after the answer (2 s on the real one)


def call_turn(app, request="what is on today?", **kw):
    kw.setdefault("model", "haiku")
    kw.setdefault("thread_id", None)
    return asyncio.run(app.state.call_turn(request, [f"Owner: {request}"], session_id=None, purpose="call", **kw))


def _main_thread(settings):
    with Session(make_engine(settings.db_url)) as s:
        return chat.main_thread(s, brain_name="claude_cli").id


# ---------------------------------------------------------------- _call_turn

def test_g5_call_turn_tells_the_text_as_it_comes_and_the_end_before_the_cli_is_done(migrated, tail):
    seen = []
    with TestClient(create_app(migrated)) as c:
        started = time.monotonic()
        text, error, sid = call_turn(c.app, on_text=lambda d: seen.append(("text", d, time.monotonic())),
                                     on_end=lambda full: seen.append(("end", full, time.monotonic())))
        done = time.monotonic()
    assert error is None and text.startswith("echo: (spoken, on a call) what is on today?") and sid
    kinds = [s[0] for s in seen]
    assert kinds.count("text") >= 2 and kinds[-1] == "end" and kinds.count("end") == 1     # the fake sends two halves
    assert "".join(s[1] for s in seen if s[0] == "text") == text
    assert seen[-1][1] == text                                                             # the end carries the final message
    assert done - seen[-1][2] >= TAIL_S * 0.7        # the CLI's own tail came after the end: nothing waited for it
    assert seen[-1][2] - started < TAIL_S + 5


def test_g5_call_turn_without_callbacks_is_what_it_was(migrated, tail):
    with TestClient(create_app(migrated)) as c:
        text, error, sid = call_turn(c.app)
    assert error is None and text.startswith("echo: (spoken, on a call) what is on today?") and sid


def test_g5_a_hand_over_to_sonnet_is_told_and_is_not_an_end(migrated):
    seen = []
    with TestClient(create_app(migrated)) as c:       # (the fake CLI hands over only in a warm process: the conversation's)
        text, error, _sid = call_turn(c.app, "hmm, what about this [[fake-escalate: judgment, which plan to drop]]",
                                      thread_id=_main_thread(migrated), on_text=lambda d: seen.append(("text", d)), on_end=lambda full: seen.append(("end", full)),
                                      on_escalate=lambda info: seen.append(("escalate", info)))
    assert error is None and "[[sonnet]]" not in text
    kinds = [s[0] for s in seen]
    assert kinds.count("escalate") == 1 and kinds.count("end") == 1                         # Haiku's end is no end
    assert kinds.index("escalate") < kinds.index("end") and kinds.index("escalate") < kinds.index("text")
    assert next(s for s in seen if s[0] == "escalate")[1]["escalated"] == "sonnet"
    assert not any("[[sonnet]]" in s[1] for s in seen if s[0] == "text")


def test_g5_a_callback_that_breaks_never_breaks_the_turn(migrated):
    def boom(*_a):
        raise RuntimeError("my bug")
    with TestClient(create_app(migrated)) as c:
        text, error, _sid = call_turn(c.app, on_text=boom, on_end=boom, on_escalate=boom)
    assert error is None and text.startswith("echo:")


# ---------------------------------------------------------------- conversation_parts, once

def test_g5_the_conversation_prompt_is_built_once_a_turn_not_twice(migrated, monkeypatch):
    built = []
    real = conversation.static_prompt
    monkeypatch.setattr(conversation, "static_prompt", lambda *a, **k: built.append(a) or real(*a, **k))
    with TestClient(create_app(migrated)) as c:
        tid = _main_thread(migrated)
        _text, error, sid = asyncio.run(c.app.state.call_turn("first question", ["Owner: first question"], session_id=None,
                                                              thread_id=tid, purpose="call", model="haiku"))
        assert error is None and sid
        built.clear()
        text, error, _ = asyncio.run(c.app.state.call_turn("second question", ["Owner: second question"], session_id=sid,
                                                           thread_id=tid, purpose="call", model="haiku"))
    assert error is None and text.startswith("echo:") and len(built) == 1


def test_g5_when_the_lasting_prompt_changed_the_session_starts_fresh_with_the_recent_messages(migrated, monkeypatch):
    built = []
    real = conversation.static_prompt
    monkeypatch.setattr(conversation, "static_prompt", lambda *a, **k: built.append(a) or real(*a, **k))
    with TestClient(create_app(migrated)) as c:
        tid = _main_thread(migrated)
        with Session(make_engine(migrated.db_url)) as s:
            chat.add_message(s, tid, "owner", "an earlier message")
            s.merge(SettingKV(key="conversation.prompt_hash", value="stale"))
            s.commit()
        text, error, _ = asyncio.run(c.app.state.call_turn("again", ["Owner: again"], session_id="old-session", thread_id=tid,
                                                           purpose="call", model="haiku"))
    assert error is None and text.startswith("echo:")
    assert len(built) == 2 and "Recent conversation" not in built[0][2] and "an earlier message" in built[1][2]


# ---------------------------------------------------------------- the switches reach a call

def test_g5_a_call_through_the_app_gives_its_speaker_the_switch_it_started_with(local, monkeypatch):   # noqa: F811
    """Read once as the call starts (voicertc.RtcSpeaker.second_piece is set per speaker, from the call's switches)."""
    _needs_pipecat()
    monkeypatch.setattr(voicertc, "ALLOW_LOOPBACK", True)
    local.app.state.openers = voicertc.OpenerCache(lambda text, lang: (b"\x00\x10" * 2400, 24000))
    seen, real = [], voicertc.RtcSpeaker.feed

    def spy(self, delta):
        seen.append(self.second_piece)
        return real(self, delta)
    monkeypatch.setattr(voicertc.RtcSpeaker, "feed", spy)
    for value in (False, True):
        assert local.put("/api/flags/g5.second_piece", json={"on": value}).status_code == 200
        seen.clear()
        asyncio.run(_app_call(local, seconds=20.0))
        assert seen and set(seen) == {value}, (value, seen)
