"""G5 hand-off at the app level (voice-recipe section 8): the switches reach a call, and what ask_kairo gets from Claude
while Claude is still working. The brain is the fake Claude CLI (tests/fakes/fake_claude.py). The voice model's side
is in test_voicemouth.py."""

from __future__ import annotations

import asyncio

from app import voicertc
from tests.test_voice_local import local  # noqa: F401  (the app with the fake voice model and Claude)
from tests.test_voice_rtc import _app_call, _needs_pipecat


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
