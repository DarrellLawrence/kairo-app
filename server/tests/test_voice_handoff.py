"""G5 hand-off at the app level (voice-recipe section 8): what ask_kairo gets from Claude while Claude is still
working. The brain is the fake Claude CLI (tests/fakes/fake_claude.py), which ends an answer with message_stop and then,
like the real one, takes FAKE_CLAUDE_TAIL_S before it sends the result. The voice model's side is in test_voicemouth.py."""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import chat, conversation, realtime, voicertc
from app.db import SettingKV, make_engine
from app.main import create_app
from app.voicemouth import FILLERS
from tests.test_voice_local import local, requests_seen  # noqa: F401  (the app with the fake voice model and Claude)
from tests.test_voice_rtc import _app_call, _needs_pipecat
from tests.test_voicelink import frames_until, utter

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


# ---------------------------------------------------------------- ask_kairo with a sink

def ask_stream(app, request="what is on today?", about="", **kw):
    sunk = []
    out = asyncio.run(app.state.mouth_ask_stream(request, [f"Owner: {request}"], about, lambda kind, value=None: sunk.append((kind, value)), **kw))
    return out, sunk


def test_g5_ask_stream_returns_what_ask_returns_and_tells_the_sink_while_claude_works(migrated, tail):
    with TestClient(create_app(migrated)) as c:
        started = time.monotonic()
        (answer, hang), sunk = ask_stream(c.app)
        took = time.monotonic() - started
        plain, plain_hang = asyncio.run(c.app.state.mouth_ask("what is on today?", ["Owner: what is on today?"], ""))
    kinds = [k for k, _ in sunk]
    assert kinds.count("text") >= 2 and kinds.count("end") == 1 and kinds[-1] == "end"
    assert (answer, hang) == (plain, plain_hang) and hang is False and answer.startswith("echo: (spoken, on a call) what is on today?")
    end_answer, end_hang = sunk[-1][1]
    assert (end_answer, end_hang) == realtime.spoken_answer("".join(v for k, v in sunk if k == "text"))   # what a call may say
    assert took >= TAIL_S * 0.7                     # (the call itself ran to the result; the sink was told at the end of the text)


def test_g5_ask_stream_gives_the_claude_session_back_to_the_conversation_like_ask_does(migrated):
    with TestClient(create_app(migrated)) as c:
        ask_stream(c.app)
        with Session(make_engine(migrated.db_url)) as s:
            first = chat.main_thread(s, brain_name="claude_cli").brain_session_id
    assert first                                                              # the next turn goes on in the same Claude session


def test_g5_the_session_is_the_conversations_before_the_cli_has_even_finished(migrated, tail):
    """The turn goes on at the end of Claude's text, up to two seconds before the result: a second question right
    away must find the same Claude session, not start a new one."""
    seen = []

    def sink(kind, value=None):
        if kind == "end":
            with Session(make_engine(migrated.db_url)) as s:
                seen.append(chat.main_thread(s, brain_name="claude_cli").brain_session_id)
    with TestClient(create_app(migrated)) as c:
        asyncio.run(c.app.state.mouth_ask_stream("what is on today?", ["Owner: what is on today?"], "", sink))
        with Session(make_engine(migrated.db_url)) as s:
            final = chat.main_thread(s, brain_name="claude_cli").brain_session_id
    assert seen and seen[0] and seen[0] == final


def test_g5_ask_stream_says_why_when_kairo_is_updating_or_paused_and_asks_nothing(migrated):
    with TestClient(create_app(migrated)) as c:
        c.app.state.maintenance_until = time.time() + 60
        (answer, hang), sunk = ask_stream(c.app)
        assert answer == "Kairo is installing an update. Try again in a minute." and hang is False and sunk == []
        c.app.state.maintenance_until = 0.0
        assert c.post("/api/kill-switch", json={"paused": True}).status_code == 200
        (answer, _hang), sunk = ask_stream(c.app)
        assert answer.startswith("Kairo is paused with the kill switch") and sunk == []


def test_g5_ask_stream_hands_a_raised_item_over_as_the_owners_answer_not_as_the_request(migrated):
    with TestClient(create_app(migrated)) as c:
        (answer, _hang), _sunk = ask_stream(c.app, "yes, I called her", about="open loop L1")
    assert "answer to Kairo's open loop" in answer and "Record it as you would in chat" in answer     # (plain speech: "your")
    assert answer.startswith("echo: (spoken, on a call) yes, I called her [")


# ---------------------------------------------------------------- the call screen warms Claude

def test_g5_opening_the_call_screen_starts_claudes_conversation_program(migrated):
    calls = []
    with TestClient(create_app(migrated)) as c:
        c.app.state.runner.brain.warm_up = lambda **kw: calls.append(kw) or True
        assert c.get("/api/voice/realtime").status_code == 200
        deadline = time.monotonic() + 10
        while not calls and time.monotonic() < deadline:
            time.sleep(0.05)
    assert calls and calls[0]["origin"] == "owner_chat"


def test_g5_the_call_screen_starts_nothing_when_the_switch_is_off(migrated):
    calls = []
    with TestClient(create_app(migrated)) as c:
        assert c.put("/api/flags/g5.warm_on_call_open", json={"on": False}).status_code == 200
        c.app.state.runner.brain.warm_up = lambda **kw: calls.append(kw) or True
        assert c.get("/api/voice/realtime").status_code == 200
        time.sleep(1.0)
    assert calls == []


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


# ---------------------------------------------------------------- a call on the old line, end to end (fake voice model, fake Claude)

def _call(local_app, *, flags=()):
    """One data question on /ws/voice. (the frames, seconds from the end of the owner's speech to the done frame)."""
    for name, on in flags:
        assert local_app.put(f"/api/flags/{name}", json={"on": on}).status_code == 200
    local_app.voice.text = "Remind me to call the dentist tomorrow"
    with local_app.websocket_connect("/ws/voice") as ws:
        started = time.monotonic()
        utter(ws)
        frames = frames_until(ws, last="done", limit=400)
        took = time.monotonic() - started
    return frames, took


def _words(frames) -> str:
    return "".join(f["text"] for f in frames if isinstance(f, dict) and f["type"] == "delta")


def _model_requests(local_app) -> int:
    return len([r for r in requests_seen(local_app) if r.get("messages")])


def test_g5_with_the_streaming_switch_claudes_words_are_spoken_as_they_are_and_the_voice_model_is_asked_once(local):   # noqa: F811
    frames, _took = _call(local, flags=[("g5.stream_verbatim", True)])
    words = _words(frames)
    assert words.startswith(FILLERS["en"][0] + " echo: (spoken, on a call) Remind me to call the dentist tomorrow")
    done = next(f for f in frames if isinstance(f, dict) and f["type"] == "done")
    assert done["text"].startswith(words.strip()[:40]) and done["server"]["tool_ms"] is not None
    assert done["server"]["streamed"] is True and done["server"]["claude_first_ms"] >= 0
    assert _model_requests(local) == 2     # the call's start (the prompt read once into the cache) and the call to ask_kairo: no second round


def test_g5_without_it_the_voice_model_says_the_answer_in_its_own_words_after_claude_is_done(local):   # noqa: F811
    frames, _took = _call(local)
    assert _words(frames).startswith(FILLERS["en"][0])
    assert _model_requests(local) == 3     # the call's start, the call to ask_kairo, then the answer in its own words


@pytest.fixture
def long_tail(monkeypatch):
    monkeypatch.setenv("KAIRO_HOOK_FAKE_TAIL_S", "2.0")      # (before the app starts: the CLI is spawned with this environment)


def test_g5_the_answer_comes_without_waiting_for_the_cli_summary_and_with_the_switch_off_it_waits(long_tail, local):   # noqa: F811
    _frames, quick = _call(local)                                         # g5.flush_at_end is on by default
    assert quick < 1.6                                                    # (the fake Claude's summary takes 2 s)
    _frames, slow = _call(local, flags=[("g5.flush_at_end", False)])
    assert slow >= 1.9


def test_g5_a_call_over_webrtc_cuts_claudes_words_once_and_with_the_switch_off_twice(local, monkeypatch):   # noqa: F811
    """The whole chain on a real (loopback) WebRTC call: the holding line, Claude's words as they come, cut into pieces."""
    _needs_pipecat()
    monkeypatch.setattr(voicertc, "ALLOW_LOOPBACK", True)
    local.app.state.openers = voicertc.OpenerCache(lambda text, lang: (b"\x00\x10" * 2400, 24000))
    assert local.put("/api/flags/g5.stream_verbatim", json={"on": True}).status_code == 200
    got = {}
    for value in (False, True):
        assert local.put("/api/flags/g5.second_piece", json={"on": value}).status_code == 200
        phone = asyncio.run(_app_call(local, seconds=25.0))
        got[value] = [e["text"] for e in phone.events if e.get("type") == "say"]
    for value, pieces in got.items():
        assert pieces[0] == FILLERS["en"][0] and pieces[1] == "echo:", (value, pieces)       # the holding line, then Claude's first piece
    assert got[False][2] == "(spoken,"                                                       # the second piece cut again at its first clause
    assert got[True][2].startswith("(spoken, on a call) Remind me to call the dentist tomorrow")   # whole sentence
    assert len(got[True]) < len(got[False])
