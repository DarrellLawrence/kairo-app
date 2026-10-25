"""G2: the call's language setting (More -> Settings -> Voice -> Language on calls) reaches Whisper on both
lines: WebRTC (voicertc) and the old WebSocket line. English only and German only decode in that language;
"English and German" (auto) leaves Whisper to find it.

The WebRTC test needs Pipecat + aiortc (the voice packages); run it with the voice Python."""

from __future__ import annotations

import asyncio

import pytest

from app import realtime, voicertc
from tests.test_voice_local import local  # noqa: F401  (the app with the fake voice model and Claude)
from tests.test_voice_rtc import OpenerCache, ToneVoice, _app_call, _needs_pipecat
from tests.test_voicelink import frames_until, utter


def watch_language(local, monkeypatch) -> list:
    """Whisper's language argument for every piece of speech the fake phone sends."""
    seen = []
    real = local.voice.transcribe_pcm
    monkeypatch.setattr(local.voice, "transcribe_pcm", lambda pcm, language=None: (seen.append(language), real(pcm, language))[1])
    return seen


def test_german_only_is_a_setting_the_owner_can_choose():
    assert realtime.LANGUAGES == ("auto", "en", "de")
    assert realtime._check("language", "de") == "de"
    with pytest.raises(realtime.RealtimeError):
        realtime._check("language", "fr")


@pytest.mark.parametrize("setting,expected", [("de", "de"), ("en", "en"), ("auto", None)])
def test_the_old_line_decodes_in_the_language_the_owner_chose(local, monkeypatch, setting, expected):
    assert local.put("/api/voice/realtime/settings", json={"language": setting}).status_code == 200
    seen = watch_language(local, monkeypatch)
    local.voice.text = "hey how are you"
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        frames_until(ws)
    assert seen and set(seen) == {expected}, seen


@pytest.mark.parametrize("setting,expected", [("de", "de"), ("en", "en"), ("auto", None)])
def test_a_webrtc_call_decodes_in_the_language_the_owner_chose(local, monkeypatch, setting, expected):
    _needs_pipecat()
    monkeypatch.setattr(voicertc, "ALLOW_LOOPBACK", True)
    assert local.put("/api/voice/realtime/settings", json={"language": setting}).status_code == 200
    seen = watch_language(local, monkeypatch)
    local.voice.text = "hey how are you"
    local.app.state.voice = ToneVoice(local.voice)
    local.app.state.openers = OpenerCache(lambda text, lang: (b"\x00\x10" * 2400, 24000))
    asyncio.run(_app_call(local, seconds=8.0))
    assert seen and set(seen) == {expected}, seen


def test_a_call_on_the_old_line_tells_the_voice_it_started(local):
    started = []
    local.voice.call_started = lambda s: started.append(s)
    local.voice.text = "hey how are you"
    with local.websocket_connect("/ws/voice") as ws:
        utter(ws)
        frames_until(ws)
    assert len(started) == 1 and hasattr(started[0], "exec")          # with a database session: it reads the switches there


def test_a_webrtc_call_tells_the_voice_it_started(local, monkeypatch):
    _needs_pipecat()
    monkeypatch.setattr(voicertc, "ALLOW_LOOPBACK", True)
    started = []
    local.voice.call_started = lambda s: started.append(s)
    local.voice.text = "hey how are you"
    local.app.state.voice = ToneVoice(local.voice)
    local.app.state.openers = OpenerCache(lambda text, lang: (b"\x00\x10" * 2400, 24000))
    asyncio.run(_app_call(local, seconds=6.0))
    assert len(started) == 1 and hasattr(started[0], "exec")
