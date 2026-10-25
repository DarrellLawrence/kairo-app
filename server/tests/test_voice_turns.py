"""Turn-taking on calls (group G6 of the voice plan, section 1): who may interrupt Kairo, when a turn ends, what a
tap does. The fake phone, Silero and Smart Turn are the real ones (see test_voice_rtc.py); Whisper and the voices are
fakes. Every switch (voicertc.Turning, flags g6.*) is tested with both values.

Timings are the sandbox's CPU (a shared 4-core machine), not the PC: the windows are wide on purpose.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from app import voicertc
from app.voicertc import Turning
from tests.test_voice_rtc import (
    FakeVoice, _call, _needs_pipecat, _speech_48k, _tone,
)
from tests.test_voice_local import local  # noqa: F401  (the app with the fake voice model and Claude)


# ---------------------------------------------------------------- 6a: a cough over Kairo is not a new question

def _ctx(*messages):
    return SimpleNamespace(messages=[{"role": r, "content": c} for r, c in messages])


def test_only_a_last_message_from_the_owner_is_something_to_answer():
    ctx = _ctx(("user", "What time is it?"), ("assistant", "Ten past two."), ("developer", "Ask them to repeat."))
    assert voicertc._last_user_text(ctx, strict=True) == ""
    assert voicertc._last_user_text(ctx) == "What time is it?"          # the old rule: the last owner message, however far back
    ctx.messages.append({"role": "user", "content": "  And tomorrow?  "})
    assert voicertc._last_user_text(ctx, strict=True) == "And tomorrow?"
    assert voicertc._last_user_text(ctx) == "And tomorrow?"
    parts = _ctx(("user", [{"type": "text", "text": "Two"}, {"type": "text", "text": "parts"}]))
    assert voicertc._last_user_text(parts, strict=True) == "Two parts"
    assert voicertc._last_user_text(_ctx(), strict=True) == "" and voicertc._last_user_text(SimpleNamespace()) == ""


class ScriptedVoice(FakeVoice):
    """Whisper hears what the script says, one entry per piece of speech; "" is a cough (no words at all)."""

    def __init__(self, *texts: str):
        super().__init__()
        self.script = list(texts)

    def transcribe_pcm(self, pcm: bytes, language=None):
        self.heard += 1
        text = self.script.pop(0) if self.script else ""
        return SimpleNamespace(text=text, language="en" if text else None, seconds=len(pcm) / 32000)


def _speech_then(gap_s: float, clip_s: float):
    """The recorded sentence, a pause of gap_s after it, then the first clip_s of it again (the sound over Kairo)."""
    import numpy as np
    speech = _speech_48k()
    return np.concatenate([speech, np.zeros(int(gap_s * 48000), dtype=np.int16), speech[:int(clip_s * 48000)]])


def _long_answer(asked: list, *, sentences: int = 40):
    async def answer(text, language, number, speaker):
        asked.append((text, number))
        for i in range(sentences if number == 1 else 0):
            speaker.feed(f"This is sentence number {i} of a long answer. ")
            await asyncio.sleep(0.02)
        if number > 1:
            speaker.feed("Again.")
        speaker.finish()
        return {"route": "talk"}
    return answer


def _after(seconds: float):
    return lambda phone: phone.mic.t0 is not None and time.monotonic() - phone.mic.t0 > seconds


def _cough_over_kairo(turning: Turning, monkeypatch) -> tuple[list, list]:
    """The owner asks something; Kairo starts a long answer; a sound over it that Whisper writes down as nothing
    (a cough). Returns what the answer was asked and what was interrupted. The watchdog is shortened for speed."""
    monkeypatch.setattr(voicertc, "TURN_STOP_TIMEOUT_OLD_S", 1.5)
    asked, stopped = [], []
    voice = ScriptedVoice("Remind me to call the dentist tomorrow morning.", "")
    voice.tone = _tone(1.5)
    asyncio.run(_call(_long_answer(asked), voice=voice, interrupt=stopped.append, turning=turning,
                      speech=_speech_then(gap_s=3.0, clip_s=0.7), wait_for=_after(11.0), timeout=20))
    return asked, stopped


def test_a_cough_over_kairo_does_not_make_it_answer_the_last_question_again(monkeypatch):
    """Measured in the research (section 1): the sound opens a turn with no words, Pipecat's recovery adds a note to
    the conversation, and Kairo answered the previous turn a second time."""
    _needs_pipecat()
    asked, stopped = _cough_over_kairo(Turning(empty_turn_fix=True), monkeypatch)
    assert asked == [("Remind me to call the dentist tomorrow morning.", 1)], asked
    assert stopped == [1]                       # the sound still cut the first answer short (the transcript-gated start is 6b)


def test_with_the_fix_off_the_cough_makes_kairo_answer_again_the_old_way(monkeypatch):
    _needs_pipecat()
    asked, _ = _cough_over_kairo(Turning(empty_turn_fix=False), monkeypatch)
    assert [n for _, n in asked] == [1, 2] and asked[0][0] == asked[1][0], asked     # the same question, answered twice


# ---------------------------------------------------------------- the app reads the switches as a call starts

def test_the_app_reads_the_g6_switches_at_the_start_of_every_call(local, monkeypatch):  # noqa: F811
    _needs_pipecat()
    from tests.test_voice_rtc import _phone_offer
    from aiortc import RTCSessionDescription
    monkeypatch.setattr(voicertc, "ALLOW_LOOPBACK", True)
    seen = []
    real = voicertc.build_call

    def spy(conn, **kwargs):
        seen.append(kwargs.get("turning"))
        return real(conn, **kwargs)
    monkeypatch.setattr(voicertc, "build_call", spy)

    async def call():
        pc = await _phone_offer()
        r = await asyncio.to_thread(local.post, "/api/rtc/offer",
                                    json={"sdp": pc.localDescription.sdp, "type": "offer", "path": "call"})
        assert r.status_code == 200, r.text
        await pc.setRemoteDescription(RTCSessionDescription(sdp=r.json()["sdp"], type=r.json()["type"]))
        await pc.close()

    async def go():
        await call()                                                    # nothing set: the defaults
        assert (await asyncio.to_thread(local.put, "/api/flags/g6.empty_turn_fix", json={"on": False})).status_code == 200
        await call()
        assert (await asyncio.to_thread(local.put, "/api/flags/g6.empty_turn_fix", json={"on": None})).status_code == 200
        await call()
    asyncio.run(go())
    assert seen == [Turning(), Turning(empty_turn_fix=False), Turning()]


# ---------------------------------------------------------------- the tap ("I'm done") under a busy PC

class RecordingVoice(FakeVoice):
    """Whisper, faked: what was said is fixed; remembers how many seconds of speech each decode was given."""

    def __init__(self):
        super().__init__()
        self.lengths: list[float] = []

    def transcribe_pcm(self, pcm: bytes, language=None):
        self.lengths.append(len(pcm) / 32000)
        return super().transcribe_pcm(pcm, language)


def _slow_words(mp, seconds: float):
    """The words reach the turn detector late: Whisper is through (nothing is pending any more) but the transcript is
    still on its way, as it is on a busy PC where every step of the pipeline waits for its turn."""
    from pipecat.frames.frames import TranscriptionFrame
    from pipecat.services.stt_service import SegmentedSTTService
    real = SegmentedSTTService.push_frame

    async def late(self, frame, *args, **kwargs):
        if isinstance(frame, TranscriptionFrame):
            await asyncio.sleep(seconds)
        return await real(self, frame, *args, **kwargs)
    mp.setattr(SegmentedSTTService, "push_frame", late)


def _slow_tap(mp, seconds: float):
    """The tap crawls through the pipeline's queues: it reaches the turn detector `seconds` after it was sent."""
    from pipecat.services.stt_service import SegmentedSTTService
    real = SegmentedSTTService.process_frame

    async def late(self, frame, direction):
        if type(frame).__name__ == "Tapped":
            await asyncio.sleep(seconds)
        return await real(self, frame, direction)
    mp.setattr(SegmentedSTTService, "process_frame", late)


def _loud_is_speech_slowly(mp, per_chunk_s: float):
    """Silero hears every loud frame as speech (the owner in a loud room) but takes per_chunk_s for each 32 ms of audio:
    on a busy PC the VAD falls behind and the audio piles up in front of it."""
    import numpy as np
    from pipecat.audio.vad.silero import SileroVADAnalyzer

    def confidence(self, buf):
        time.sleep(per_chunk_s)
        return 1.0 if np.abs(np.frombuffer(buf, np.int16)).max(initial=0) > 1000 else 0.0
    mp.setattr(SileroVADAnalyzer, "voice_confidence", confidence)


def _tapped(turning: Turning, *, words_late_s: float = 0.0, tap_late_s: float = 0.0, vad_s: float = 0.0, tap_at: float = 3.0):
    """The owner talks in a loud room (the VAD never hears a pause) from 0.5 s and taps "done" at tap_at seconds."""
    from tests.test_voice_rtc import _loud_is_speech, _steady
    _needs_pipecat()
    asked = []

    async def answer(text, language, number, speaker):
        asked.append((text, number, time.monotonic()))
        speaker.finish("Okay.")
        return {"route": "talk"}

    voice = RecordingVoice()
    with pytest.MonkeyPatch.context() as mp:
        _loud_is_speech(mp)
        if vad_s:
            _loud_is_speech_slowly(mp, vad_s)
        if words_late_s:
            _slow_words(mp, words_late_s)
        if tap_late_s:
            _slow_tap(mp, tap_late_s)
        phone = asyncio.run(_call(answer, voice=voice, speech=_steady(14.0), turning=turning,
                                  later=((tap_at, {"type": "done"}, "speech"),), wait_for=lambda p: bool(asked), timeout=14))
    return SimpleNamespace(phone=phone, voice=voice, asked=asked,
                           wait=(asked[0][2] - phone.sent[0][0]) if asked and phone.sent else None)


def test_a_tap_that_gets_ahead_of_the_words_still_ends_the_turn():
    """The flake: under load (load 7 on 4 cores) the tap ended the turn at once in about 3 of 4 runs, and in the rest it
    was thrown away as 'nothing was said' and the answer came 7 s later. Cause: the tap reaches the turn detector while
    Whisper is through (nothing pending) but its transcript has not arrived yet, and the detector took 'no transcript
    yet' for 'no words'. Here the tap is held up 0.3 s and the words 0.8 s on purpose, so it happens every time."""
    r = _tapped(Turning(tap_lag_fix=True), words_late_s=0.8, tap_late_s=0.3)
    assert [(text, n) for text, n, _ in r.asked] == [(r.voice.text, 1)], r.phone.events
    assert r.wait < 2.5, "answered right after the tap"
    transcript = next(e for e in r.phone.events if e.get("type") == "transcript")
    assert transcript["ended"] == "tap" and transcript["server"]["vad_ms"] == 0
    assert len(r.voice.lengths) == 1                              # written down once


def test_with_the_tap_fix_off_a_tap_that_gets_ahead_of_the_words_is_lost_the_old_way():
    r = _tapped(Turning(tap_lag_fix=False), words_late_s=0.8, tap_late_s=0.3)
    assert r.wait is None or r.wait > 2.5, "the tap was taken (the old code lost it)"


def test_a_tap_keeps_the_words_said_before_it_when_the_vad_is_behind():
    """The owner talks from 0.5 s and taps at 3.0 s. The VAD is about a second behind by then (it takes 45 ms for each
    32 ms of audio). With the fix the speech ends where the owner tapped: Whisper gets the whole 2.5 s. The old code let
    the VAD go deaf at the moment of the tap, wherever it had got to, and the last second of words was lost."""
    on, off = _tapped(Turning(tap_lag_fix=True), vad_s=0.045), _tapped(Turning(tap_lag_fix=False), vad_s=0.045)
    assert on.asked and off.asked, (on.phone.events, off.phone.events)
    assert on.voice.lengths[0] >= 2.3, (on.voice.lengths, off.voice.lengths)       # 0.5 s to 3.0 s, minus the VAD's start
    assert on.voice.lengths[0] > off.voice.lengths[0] + 0.4, (on.voice.lengths, off.voice.lengths)
    assert on.wait < 5.0, on.wait                                  # bounded: the VAD catches up, then the turn ends
