"""Turn-taking on calls (group G6 of the voice plan, section 1): who may interrupt Kairo, when a turn ends, what a
tap does. The fake phone, Silero and Smart Turn are the real ones (see test_voice_rtc.py); Whisper and the voices are
fakes. Every switch (voicertc.Turning, flags g6.*) is tested with both values.

Timings are the sandbox's CPU (a shared 4-core machine), not the PC: the windows are wide on purpose.
"""

from __future__ import annotations

import asyncio
import logging
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
        self.lengths_s: list[float] = []

    def transcribe_pcm(self, pcm: bytes, language=None):
        self.heard += 1
        self.lengths_s.append(len(pcm) / 32000)
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
    asked, stopped = _cough_over_kairo(Turning(empty_turn_fix=True, transcript_barge_in=False), monkeypatch)
    assert asked == [("Remind me to call the dentist tomorrow morning.", 1)], asked
    assert stopped == [1]                       # the old start: any sound cut the first answer short (g6.transcript_barge_in fixes that)


def test_with_the_fix_off_the_cough_makes_kairo_answer_again_the_old_way(monkeypatch):
    _needs_pipecat()
    asked, _ = _cough_over_kairo(Turning(empty_turn_fix=False, transcript_barge_in=False), monkeypatch)
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


# ---------------------------------------------------------------- Smart Turn waits up to 3 s (g6.smart_turn_3s)

def _smart_turn_says_not_done(mp):
    """The real Smart Turn model says 'not finished' about whatever it hears (a thought that trails off)."""
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
    mp.setattr(LocalSmartTurnAnalyzerV3, "_predict_endpoint", lambda self, audio: {"prediction": 0, "probability": 0.2})


class _Stamped:
    """Log lines kairo.voicertc wrote, with the time each was written."""

    def __init__(self):
        self.rows: list[tuple[float, str]] = []
        outer = self

        class H(logging.Handler):
            def emit(self, record):
                outer.rows.append((time.monotonic(), record.getMessage()))
        self.handler = H(level=logging.INFO)
        self.log = logging.getLogger("kairo.voicertc")

    def __enter__(self):
        self.old = self.log.level
        self.log.setLevel(logging.INFO)
        self.log.addHandler(self.handler)
        return self

    def __exit__(self, *exc):
        self.log.removeHandler(self.handler)
        self.log.setLevel(self.old)


def _unfinished_thought(turning: Turning, *, silence_s: float = 2.0, watchdog_s: float = 2.0):
    """The owner says a sentence the turn detector thinks is not finished and then says nothing more. Returns when the
    answer was asked, counted from the end of the speech (the shortened constants stand for 3.0 s)."""
    _needs_pipecat()
    asked = []

    async def answer(text, language, number, speaker):
        asked.append(time.monotonic())
        speaker.finish("Okay.")
        return {"route": "talk"}

    with pytest.MonkeyPatch.context() as mp:
        _smart_turn_says_not_done(mp)
        mp.setattr(voicertc, "SMART_TURN_SILENCE_S", silence_s)
        mp.setattr(voicertc, "TURN_STOP_TIMEOUT_S", watchdog_s)
        phone = asyncio.run(_call(answer, turning=turning, wait_for=lambda p: bool(asked), timeout=20))
    assert asked, phone.events
    transcript = next(e for e in phone.events if e.get("type") == "transcript")
    return asked[0] - phone.mic.ends[0], transcript


def test_a_thought_that_trails_off_waits_the_long_pause_when_the_switch_is_on():
    """Smart Turn says 'not done', the owner stays quiet: the turn ends after SMART_TURN_SILENCE_S (3.0 s; shortened to
    2.0 s here), counted from the VAD's stop, not after the old 0.5 s. Cost in the research: +2.5 s on the 3.8 % of
    finished turns Smart Turn takes for unfinished, +0.096 s on the mean turn."""
    on_wait, on_event = _unfinished_thought(Turning(smart_turn_3s=True))
    off_wait, off_event = _unfinished_thought(Turning(smart_turn_3s=False))
    assert 2.0 < on_wait < 3.6, (on_wait, off_wait)                       # 0.2 s VAD + 2.0 s + the answer's start
    assert 0.4 < off_wait < 1.6, (on_wait, off_wait)                      # the old 0.5 s
    assert on_event.get("ended") == "silence" and off_event.get("ended") == "silence"   # no tap, no watchdog: the quiet ended it


def test_a_turn_with_no_words_ends_at_the_watchdog_and_the_switch_picks_which():
    """A sound Whisper writes nothing for opens a turn nothing can end except the watchdog: 3.0 s with the switch on
    (Pipecat's own 5.0 s off). The call says so in its log."""
    _needs_pipecat()
    ends = {}
    for on in (True, False):
        voice = ScriptedVoice("")
        with _Stamped() as seen, pytest.MonkeyPatch.context() as mp:
            mp.setattr(voicertc, "TURN_STOP_TIMEOUT_S", 1.0)
            mp.setattr(voicertc, "TURN_STOP_TIMEOUT_OLD_S", 2.5)
            phone = asyncio.run(_call(lambda *a: None, voice=voice, turning=Turning(smart_turn_3s=on),
                                      wait_for=_after(8.0), timeout=14))
        hits = [t for t, line in seen.rows if "watchdog" in line]
        assert hits, [line for _, line in seen.rows]
        ends[on] = hits[0] - phone.mic.ends[0]
    assert ends[False] - ends[True] > 1.0, ends                           # the switch decides how long the watchdog waits


# ---------------------------------------------------------------- ignore windows (g6.ignore_windows)

def test_a_window_holds_new_speech_for_the_first_moments_of_kairos_voice_and_after_a_gap_in_the_audio():
    s = voicertc._State(answer=None, interrupt=lambda n: None, events=lambda m: None, language_mode="auto")
    assert s.holding(100.0) is False                              # nothing going on: speech is heard at once
    s.bot_speaking, s.bot_started = True, 100.0
    assert s.holding(100.0) and s.holding(100.0 + voicertc.BARGE_GRACE_S - 0.01)      # Kairo's first moments: its start in the mic
    assert not s.holding(100.0 + voicertc.BARGE_GRACE_S + 0.01)
    s.bot_speaking = False
    assert not s.holding(100.1)                                   # Kairo is quiet: no window
    s.note_audio(200.0)                                           # audio arriving every 20 ms: nothing
    s.note_audio(200.02)
    assert s.holding(200.03) is False
    s.note_audio(200.02 + voicertc.AUDIO_GAP_S + 0.1)             # the audio stalled (a reconnect, a glitch) and came back
    t = 200.02 + voicertc.AUDIO_GAP_S + 0.1
    assert s.holding(t + 0.01) and s.holding(t + voicertc.RECONNECT_HOLD_S - 0.01)
    assert not s.holding(t + voicertc.RECONNECT_HOLD_S + 0.01)


def _burst_over_kairo(turning: Turning, *, burst_after_s: float, burst_s: float, grace_s: float):
    """The owner asks something, Kairo starts a long answer, and the owner makes a sound (the first burst_s seconds of the
    same sentence) burst_after_s after the question ended. Whisper writes the sound down as 'Stop'."""
    _needs_pipecat()
    asked, stopped = [], []
    voice = ScriptedVoice("Remind me to call the dentist tomorrow morning.", "Stop")
    voice.tone = _tone(1.5)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(voicertc, "BARGE_GRACE_S", grace_s)
        phone = asyncio.run(_call(_long_answer(asked), voice=voice, interrupt=stopped.append, turning=turning,
                                  speech=_speech_then(gap_s=burst_after_s, clip_s=burst_s), wait_for=_after(8.0), timeout=14))
    return SimpleNamespace(phone=phone, voice=voice, asked=asked, stopped=stopped,
                           speech_events=[e for e in phone.events if e.get("type") == "speech"])


def test_a_sound_in_the_first_moments_of_kairos_voice_is_not_heard_with_the_window_on():
    """The start of Kairo's own voice is when its echo is loudest and the owner's speech overlaps it by accident: a new
    stretch of speech inside the window is not reported (the window is shortened to 3 s here so the test can hit it)."""
    on = _burst_over_kairo(Turning(ignore_windows=True), burst_after_s=1.0, burst_s=0.6, grace_s=3.0)
    off = _burst_over_kairo(Turning(ignore_windows=False), burst_after_s=1.0, burst_s=0.6, grace_s=3.0)
    assert on.voice.heard == 1 and on.stopped == [] and len(on.speech_events) == 1, (on.voice.heard, on.stopped, on.phone.events)
    assert off.voice.heard == 2 and off.stopped == [1], (off.voice.heard, off.stopped)        # the old way: heard, and it cut Kairo off


def test_speech_that_goes_on_past_the_window_is_heard_from_its_start():
    """Not a sound but the owner taking over: it starts inside the window and goes on, so it is reported when the window
    closes, and Whisper still gets the words from the start (the speech-to-text keeps the last second before it)."""
    on = _burst_over_kairo(Turning(ignore_windows=True), burst_after_s=0.4, burst_s=2.5, grace_s=1.5)
    assert on.voice.heard == 2, (on.voice.heard, on.phone.events)
    assert on.stopped == [1]                                      # and it did cut the long answer off
    assert on.voice.lengths_s[1] >= 1.8, on.voice.lengths_s       # the whole sentence, not just what came after the window


def test_speech_already_going_on_is_never_cut_by_a_window():
    """The owner is mid-sentence (the loud room, no pause) when Kairo says a line: the window must not end their speech."""
    from tests.test_voice_rtc import _loud_is_speech, _steady
    _needs_pipecat()
    voice = RecordingVoice()
    asked = []

    async def answer(text, language, number, speaker):
        asked.append(time.monotonic())
        speaker.finish("Okay.")
        return {"route": "talk"}
    with pytest.MonkeyPatch.context() as mp:
        _loud_is_speech(mp)
        mp.setattr(voicertc, "BARGE_GRACE_S", 100.0)               # the window stays open for the whole test
        phone = asyncio.run(_call(answer, voice=voice, speech=_steady(5.0), turning=Turning(ignore_windows=True),
                                  later=((2.0, {"type": "say", "text": "One moment."}),), wait_for=lambda p: bool(asked),
                                  timeout=14))
    assert asked and voice.lengths[0] >= 4.0, (voice.lengths, phone.events)      # one decode of the whole 5 s


def test_the_first_second_after_the_call_connects_is_a_window_too():
    """Connecting (or reconnecting) the phone's audio starts with a burst of noise: RECONNECT_HOLD_S after the client
    connected, new speech is not reported (shortened to 2.0 s here); speech that goes on is heard."""
    _needs_pipecat()
    first = {}
    for on in (True, False):
        voice = RecordingVoice()
        asked = []

        async def answer(text, language, number, speaker, asked=asked):
            asked.append(time.monotonic())
            speaker.finish("Okay.")
            return {"route": "talk"}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(voicertc, "RECONNECT_HOLD_S", 2.0)
            phone = asyncio.run(_call(answer, voice=voice, times=(0.2,), turning=Turning(ignore_windows=on),
                                      wait_for=lambda p: bool(asked), timeout=14))
        i = next(k for k, e in enumerate(phone.events) if e.get("type") == "speech")
        first[on] = phone.event_at[i] - phone.mic.t0
        assert asked
    assert first[True] > first[False] + 0.8, first                # held until the window closed


# ---------------------------------------------------------------- barge-in only on words (g6.transcript_barge_in)

def _kairo_lines(asked: list, lines: list[str], second: str = ""):
    """An answer of fixed lines (each played as a 3 s tone); later turns get `second` (or nothing)."""
    async def answer(text, language, number, speaker):
        asked.append((text, number))
        if number == 1:
            for line in lines:
                speaker.feed(line + " ")
                await asyncio.sleep(0.02)
        elif second:
            speaker.feed(second)
        speaker.finish()
        return {"route": "talk"}
    return answer


KAIRO_SAYS = ["Your next meeting is at three with the design team.", "I added milk to the shopping list.",
              "It will be sunny tomorrow, around eighteen degrees.", "The train to Zurich leaves at eight fifteen.",
              "Do you want me to move the dentist appointment to Thursday?", "I can't find anything about that in your notes."]


def _over_kairo(over_says: str, turning: Turning = Turning(), *, after_s: float = 3.0, clip_s: float = 0.8,
                lines=KAIRO_SAYS, seconds: float = 10.0):
    """The owner asks something, Kairo starts a long answer (every line a 3 s tone), and the owner makes a sound over it
    that Whisper writes down as `over_says`. Returns what the call did."""
    _needs_pipecat()
    asked, stopped = [], []
    voice = ScriptedVoice("Remind me to call the dentist tomorrow morning.", over_says)
    voice.tone = _tone(3.0)
    phone = asyncio.run(_call(_kairo_lines(asked, lines), voice=voice, interrupt=stopped.append,
                              turning=turning, speech=_speech_then(gap_s=after_s, clip_s=clip_s), wait_for=_after(seconds),
                              timeout=seconds + 8))
    kinds = [e.get("type") for e in phone.events]
    at = {k: phone.event_at[kinds.index(k)] - phone.mic.t0 for k in ("barge_in",) if k in kinds}
    return SimpleNamespace(phone=phone, voice=voice, asked=asked, stopped=stopped, kinds=kinds, at=at,
                           heard=[e for e in phone.events if e.get("type") == "heard"])


@pytest.mark.parametrize("says", ["Mhm.", "Yeah, okay.", "Tuesday."])
def test_a_sound_of_agreement_over_kairo_does_not_stop_it(says):
    r = _over_kairo(says)
    assert r.stopped == [] and "barge_in" not in r.kinds, (r.stopped, r.kinds)
    assert [n for _, n in r.asked] == [1], r.asked                 # not answered, nothing new started
    assert len(r.heard) == 1 and r.voice.heard == 2                # written down, but the phone and the voice model never heard it
    assert r.kinds.count("speech") == 1                            # the phone was not told "you are talking" for it
    assert any(t > 7.0 for t in r.phone.loud), "Kairo went on talking"


@pytest.mark.parametrize("says", ["No wait, Tuesday.", "Stop.", "Make it five o'clock instead."])
def test_words_over_kairo_stop_it_at_the_transcript_and_are_answered(says):
    r = _over_kairo(says)
    assert r.stopped == [1] and r.kinds.count("barge_in") == 1, (r.stopped, r.kinds)
    assert [n for _, n in r.asked] == [1, 2] and r.asked[1][0] == says         # the new turn is the words, answered
    assert len(r.heard) == 2 and r.heard[1]["text"] == says
    b = r.at["barge_in"]
    assert not [t for t in r.phone.loud if b + 0.7 < t < b + 3.5], "Kairo went quiet after the barge-in"
    assert any(3.5 < t < b for t in r.phone.loud), "...and had been talking until then"


def test_kairos_own_words_coming_back_through_the_microphone_do_not_stop_it():
    """Its echo, written down by Whisper (80 % of the words are in what Kairo just said), is not the owner."""
    r = _over_kairo("meeting is at three with the design team")
    assert r.stopped == [] and "barge_in" not in r.kinds, (r.stopped, r.kinds)
    assert [n for _, n in r.asked] == [1]
    # ... while words of its own make it the owner: only 2 of 7 are in what Kairo said
    r = _over_kairo("no, make the meeting five o'clock instead")
    assert r.stopped == [1], (r.stopped, r.kinds)


def test_a_cough_over_kairo_neither_stops_it_nor_makes_it_answer_again(monkeypatch):
    monkeypatch.setattr(voicertc, "TURN_STOP_TIMEOUT_S", 1.5)
    r = _over_kairo("", seconds=12.0)
    assert r.stopped == [] and [n for _, n in r.asked] == [1] and "barge_in" not in r.kinds


def test_with_the_switch_off_any_sound_over_kairo_stops_it_the_old_way_and_the_phone_is_still_told():
    r = _over_kairo("Mhm.", Turning(transcript_barge_in=False, ignore_windows=False))
    assert r.stopped == [1], (r.stopped, r.kinds)                  # the VAD's start alone, as before
    assert r.kinds.count("barge_in") == 1                          # g7.listen_on_barge_in relies on the PC saying so either way
    assert r.kinds.count("speech") == 2                            # and every stretch of speech told the phone


def test_a_barge_in_is_told_to_the_phone_once_and_only_when_kairos_voice_was_playing():
    r = _over_kairo("Stop.")
    assert r.kinds.count("barge_in") == 1
    quiet = _over_kairo("Stop.", Turning(), after_s=0.0, lines=[])      # nothing for Kairo to say: the words are just a turn
    assert "barge_in" not in quiet.kinds and quiet.stopped in ([], [1])


def test_a_backchannel_over_kairo_leaves_nothing_behind_that_stops_the_next_real_interruption():
    """'Mhm' over Kairo opens a turn nothing will answer; it closes (or is still waiting for the quiet) when the owner
    really breaks in a few seconds later, and that is heard and obeyed all the same."""
    import numpy as np
    _needs_pipecat()
    speech = _speech_48k()
    gap = np.zeros(int(2.0 * 48000), dtype=np.int16)
    track = np.concatenate([speech, np.zeros(int(2.5 * 48000), dtype=np.int16), speech[:int(0.7 * 48000)], gap,
                            speech[:int(0.9 * 48000)]])
    asked, stopped = [], []
    voice = ScriptedVoice("Remind me to call the dentist tomorrow morning.", "Mhm.", "No wait, Tuesday.")
    voice.tone = _tone(3.0)
    phone = asyncio.run(_call(_kairo_lines(asked, KAIRO_SAYS), voice=voice, interrupt=stopped.append, speech=track,
                              wait_for=lambda p: bool(stopped) and len(asked) >= 2, timeout=20))
    kinds = [e.get("type") for e in phone.events]
    assert stopped == [1] and kinds.count("barge_in") == 1, (stopped, kinds)
    assert [t for t, _ in asked] == ["Remind me to call the dentist tomorrow morning.", "No wait, Tuesday."], asked
