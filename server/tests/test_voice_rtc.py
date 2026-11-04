"""Voice V3 (owner's request 2026-10-16): calls over WebRTC on Pipecat (app/voicertc.py).

The pure parts (first clause, the speaker, the tailnet filter, the turn clock) run everywhere. The call
itself runs with Pipecat + aiortc installed (the voice packages): a fake phone (aiortc) sends real
speech from a file over WebRTC, the real Silero VAD and Smart Turn v3 decide the turn, Whisper and the
voices are fakes, and the phone checks that Kairo's audio comes back, with the steps timed.
"""

from __future__ import annotations

import asyncio
import fractions
import io
import json
import logging
import math
import random
import statistics
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import rtcjitter, voicertc
from app.voicelink import first_clause
from app.voicertc import OpenerCache, RtcSpeaker, Timeline, Turning, offer_on_tailnet, on_tailnet

SPEECH = Path(__file__).parent / "fixtures" / "speech_en.wav"    # "Remind me to call the dentist tomorrow morning."


# ---------------------------------------------------------------- pure parts

def test_the_first_piece_goes_at_the_first_clause_or_five_words():
    assert first_clause("Sure, I'll") == len("Sure,")
    assert first_clause("Okay. ") == len("Okay.")
    assert first_clause("Okay.") is None                                 # maybe "Okay.5": wait for the space
    assert first_clause("Your next train leaves at") is None           # 5 words, but no space after the 5th yet
    assert first_clause("Your next train leaves at ") == len("Your next train leaves at ")
    assert first_clause("The 7.42") is None                              # a dot inside a number is no end
    assert first_clause("Dr. Weber says ") is None
    assert first_clause("") is None


def _drain(speaker: RtcSpeaker) -> list[str]:
    out = []
    while not speaker.queue.empty():
        piece = speaker.queue.get_nowait()
        if piece is None:
            break
        out.append(piece)
    return out


def test_the_speaker_says_the_first_clause_then_sentences_and_kairos_lines_in_order():
    s = RtcSpeaker(1)
    for piece in ("Sure", ", I'll", " remind you tomorrow", " morning. And I", " added it to your list."):
        s.feed(piece)
    s.say("Btw, did you pay the phone bill?")
    s.feed("It's due today.")
    s.finish()
    assert _drain(s) == ["Sure,", "I'll remind you tomorrow morning. And I added it to your list.",   # after the first piece:
                         "Btw, did you pay the phone bill?", "It's due today."]                        # sentences of 40+ letters


def test_the_speaker_says_a_whole_answer_that_came_at_once_and_nothing_after_stop():
    s = RtcSpeaker(2)
    s.finish("All done.")
    assert _drain(s) == ["All done."]
    s = RtcSpeaker(3)
    s.feed("Sure, ")
    s.stop()
    s.feed("more words. ")
    s.say("A line.")
    s.finish()
    assert _drain(s) == ["Sure,"]


# ---------------------------------------------------------------- G5: where the pieces are cut

CLAUDE_SAYS = ("Your next train leaves at 14:32 from platform two and arrives in Bern at 15:10. "
               "Then you have a free hour. Enjoy it.")


def _in_pieces(text: str, n: int = 7) -> list[str]:
    return [text[i:i + n] for i in range(0, len(text), n)]


def _say(speaker: RtcSpeaker, text: str, *, holding_line: str | None = "Okay, one moment.") -> list[str]:
    if holding_line:
        speaker.say(holding_line)                       # Kairo's own line: the answer's words start fresh after it
    for delta in _in_pieces(text):
        speaker.feed(delta)
    speaker.finish()
    return _drain(speaker)


def test_g5_the_second_piece_is_not_cut_at_five_words_again():
    """Measured: the first piece is five words (the owner's rule), but the second was cut at five words too, in the middle
    of a sentence, so every answer was cut twice. Now: the first piece, then whole sentences."""
    got = _say(RtcSpeaker(1), CLAUDE_SAYS)
    assert got == ["Okay, one moment.", "Your next train leaves at",
                   "14:32 from platform two and arrives in Bern at 15:10. Then you have a free hour.", "Enjoy it."]
    cuts = [p for p in got[1:] if p[-1] not in ".!?…"]
    assert cuts == ["Your next train leaves at"]         # one cut mid-sentence per answer, not two


def test_g5_the_second_piece_fix_has_a_switch_and_off_is_the_old_cutting():
    s = RtcSpeaker(1)
    s.second_piece = False
    assert _say(s, CLAUDE_SAYS) == ["Okay, one moment.", "Your next train leaves at", "14:32 from platform two and",
                                    "arrives in Bern at 15:10. Then you have a free hour.", "Enjoy it."]
    assert RtcSpeaker(2).second_piece is True            # the plain fix is the default of the class


def test_g5_the_second_piece_fix_also_holds_without_a_holding_line():
    got = _say(RtcSpeaker(1), CLAUDE_SAYS, holding_line=None)
    assert got == ["Your next train leaves at",
                   "14:32 from platform two and arrives in Bern at 15:10. Then you have a free hour.", "Enjoy it."]


def test_g5_a_comma_in_the_first_words_is_still_the_first_piece():
    got = _say(RtcSpeaker(1), "Sure, your next train leaves at 14:32 from platform two and arrives in Bern.", holding_line=None)
    assert got == ["Sure,", "your next train leaves at 14:32 from platform two and arrives in Bern."]


def test_g5_flush_lets_the_last_words_out_without_closing_the_speaker():
    s = RtcSpeaker(1)
    for delta in _in_pieces("Your train is on time. See you"):
        s.feed(delta)
    assert _drain(s) == ["Your train is on time."]             # "See you" has no end yet: it is still being collected
    s.flush()
    assert not s.closed and not s.stopped
    assert _drain(s) == ["See you"]                            # the end of Claude's text: nothing waits for the CLI's summary
    s.feed("Anything else? Just ask me later.")                # words after a flush start a fresh first piece
    s.finish()
    assert _drain(s) == ["Anything else?", "Just ask me later."]


def test_g5_flush_sends_a_first_piece_that_never_reached_five_words():
    s = RtcSpeaker(1)
    s.feed("Okay")
    assert s.queue.empty()
    s.flush()
    assert _drain(s) == ["Okay"]


def test_g5_flush_after_stop_or_finish_says_nothing_more():
    s = RtcSpeaker(1)
    s.feed("Sure, one")
    s.stop()
    s.flush()
    assert _drain(s) == ["Sure,"]
    s = RtcSpeaker(2)
    s.finish("Done.")
    s.flush()
    assert _drain(s) == ["Done."]


def test_only_tailnet_addresses_are_used():
    assert on_tailnet("100.101.102.103") and on_tailnet("fd7a:115c:a1e0::1")
    assert not on_tailnet("192.168.1.20") and not on_tailnet("85.1.2.3") and not on_tailnet("abc.local")
    offer = "\r\n".join([
        "v=0", "m=audio 9 UDP/TLS/RTP/SAVPF 111",
        "a=candidate:1 1 udp 2122260223 192.168.1.20 50000 typ host",
        "a=candidate:2 1 udp 2122194687 100.90.1.2 50001 typ host",
        "a=candidate:3 1 udp 2122129151 6b1c-uuid.local 50002 typ host",
        "a=candidate:4 1 udp 1686052607 85.1.2.3 50003 typ srflx raddr 192.168.1.20 rport 50000",
        "a=candidate:5 1 tcp 1518280447 100.90.1.2 9 typ host tcptype active",
        "a=end-of-candidates", ""])
    kept = offer_on_tailnet(offer)
    assert "100.90.1.2 50001" in kept and "a=end-of-candidates" in kept and "m=audio" in kept
    for gone in ("192.168.1.20 50000", "6b1c-uuid.local", "85.1.2.3", "tcptype"):
        assert gone not in kept


def test_the_turn_clock_counts_from_the_end_of_speech():
    t = Timeline(vad_stop=10.2, stt_start=10.2, stt_done=10.35, turn_end=10.36, first_token=10.5,
                 tts_start=10.5, first_audio=10.52, cached=True)
    ms = t.ms()
    assert ms["vad_ms"] == 200 and ms["stt_ms"] == 150 and ms["turn_at_ms"] == 360
    assert ms["first_token_at_ms"] == 500 and ms["first_audio_at_ms"] == 520 and ms["llm_ms"] == 140
    assert ms["cached_opener"] is True
    assert Timeline().ms() == {}


def test_a_tapped_turn_is_timed_from_the_tap():
    """The owner tapped "done" at 10.0 (the VAD's speech was cut 20 ms later): no pause was waited for."""
    ms = Timeline(vad_stop=10.02, stt_start=10.02, stt_done=10.14, turn_end=10.15, tapped=10.0, first_token=10.3,
                  tts_start=10.3, first_audio=10.32).ms()
    assert ms["vad_ms"] == 0 and ms["turn_at_ms"] == 150 and ms["first_audio_at_ms"] == 320 and ms["stt_ms"] == 120


def test_a_tap_counts_only_when_something_was_heard_and_never_holds_the_call_for_long():
    s = voicertc._State(answer=None, interrupt=lambda n: None, events=lambda m: None, language_mode="auto")
    assert s.tap_now() is False and s.tapping() is None          # nothing heard since the last turn
    for heard in ({"vad_on": True}, {"pending": 1}, {"heard": "remind me"}):
        s = voicertc._State(answer=None, interrupt=lambda n: None, events=lambda m: None, language_mode="auto")
        for k, v in heard.items():
            setattr(s, k, v)
        assert s.tap_now() is True and s.tapping() == "waiting", heard
        assert s.tap_now() is False                               # one tap at a time
    s.pending, s.tap_at = 2, s.tap_at - voicertc.TAP_HOLD_S - 0.1   # never taken (Whisper stuck): let go
    assert s.tapping() is None and s.pending == 0


def test_the_opener_cache_keeps_short_lines_only():
    made = []

    def speak(text, lang):
        made.append((text, lang))
        return b"\x01\x00" * 10, 24000
    c = OpenerCache(speak)
    assert c.warm({"en": ("Sure,", "Okay."), "de": ("Klar,",)}) == 3
    assert c.get("  sure, ", "en") == (b"\x01\x00" * 10, 24000) and c.get("Sure,", "de") is None
    c.put("This is a much longer sentence, too long.", "en", b"", 24000)
    assert c.get("This is a much longer sentence, too long.", "en") is None
    assert c.warm({"en": ("Sure,",)}) == 0 and len(made) == 3


# ---------------------------------------------------------------- the call (needs the voice packages)

def _tone(seconds: float = 0.4, rate: int = 24000) -> bytes:
    n = int(seconds * rate)
    pcm = b"".join(int(8000 * math.sin(2 * math.pi * 440 * i / rate)).to_bytes(2, "little", signed=True) for i in range(n))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class FakeVoice:
    """Whisper and the voices, faked: what the owner said is fixed; every line is a short tone."""

    def __init__(self, text: str = "Remind me to call the dentist tomorrow morning."):
        self.text, self.spoken, self.heard = text, [], 0
        self.tone = _tone()

    def transcribe_pcm(self, pcm: bytes, language=None):
        self.heard += 1
        return SimpleNamespace(text=self.text, language="en", seconds=len(pcm) / 32000)

    def speak(self, text: str, language=None):
        self.spoken.append(text)
        return self.tone, language or "en"


def _speech_48k():
    import numpy as np
    with wave.open(str(SPEECH)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
        rate = w.getframerate()
    y = np.interp(np.linspace(0, len(x) - 1, int(len(x) * 48000 / rate)), np.arange(len(x)), x).astype(np.int16)
    return y


def _phone_track(times: list[float], speech=None):
    """The phone's microphone: half a second of room, then speech at each of `times` (seconds after the
    start), silence otherwise; real-time pace, 20 ms frames at 48 kHz. speech: 48 kHz samples (default:
    the recorded sentence)."""
    import numpy as np
    from aiortc import MediaStreamTrack
    from av import AudioFrame
    speech = _speech_48k() if speech is None else speech

    class Mic(MediaStreamTrack):
        kind = "audio"

        def __init__(self):
            super().__init__()
            self.t0, self.n = None, 0
            self.ends: list[float] = []          # monotonic time each speech ended

        async def recv(self):
            if self.t0 is None:
                self.t0 = time.monotonic()
            self.n += 1
            await asyncio.sleep(max(0.0, self.t0 + self.n * 0.02 - time.monotonic()))
            at = (self.n - 1) * 960
            chunk = np.zeros(960, dtype=np.int16)
            for k, start in enumerate(times):
                i = at - int(start * 48000)
                if 0 <= i < len(speech):
                    part = speech[i:i + 960]
                    chunk[:len(part)] = part
                    if i + 960 >= len(speech) and len(self.ends) <= k:
                        self.ends.append(time.monotonic())
            f = AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
            f.sample_rate, f.pts, f.time_base = 48000, self.n * 960, fractions.Fraction(1, 48000)
            return f
    return Mic()


async def _call(answer, *, times=(0.5,), wait_for=lambda phone: phone.audio_after, timeout=25.0, interrupt=None,
                voice=None, openers=None, greeting=None, first_message=None, warning=None, speech=None, later=(),
                gate=None, turning=None, speculate=None, discard=None):
    """A fake phone calls Kairo over WebRTC on this machine. Returns what the phone saw.
    later: (seconds after the mic started, message[, event]) sent on the data channel then, once the PC sent
    that event (the phone taps "done" only after the PC said it hears speech); phone.sent: when."""
    import numpy as np
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    voicertc.tailnet_only(allow_loopback=True)      # the fake phone is in this process: it offers 127.0.0.1 too
    voice = voice or FakeVoice()
    phone = SimpleNamespace(events=[], audio_after=[], mic=None, loud=[], sent=[], event_at=[])
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))   # like the phone: no STUN/TURN (aiortc's default asks Google)
    mic = _phone_track(list(times), speech)
    phone.mic = mic
    pc.addTrack(mic)
    channel = pc.createDataChannel("kairo")

    @channel.on("message")
    def on_message(msg):
        try:
            phone.events.append(json.loads(msg))
            phone.event_at.append(time.monotonic())
        except ValueError:
            pass

    async def pings():
        sent = False
        while True:
            await asyncio.sleep(0.5)
            if channel.readyState == "open":
                channel.send("ping")
                if first_message is not None and not sent:
                    channel.send(json.dumps(first_message))
                    sent = True

    async def script():
        for at, msg, *after in later:
            while (mic.t0 is None or channel.readyState != "open"
                   or (after and not any(e.get("type") == after[0] for e in phone.events))):
                await asyncio.sleep(0.01)
            await asyncio.sleep(max(0.0, mic.t0 + at - time.monotonic()))
            channel.send(json.dumps(msg))
            phone.sent.append((time.monotonic(), msg))

    @pc.on("track")
    def on_track(track):
        async def read():
            while True:
                try:
                    f = await track.recv()
                except Exception:
                    return
                loud = np.abs(f.to_ndarray()).max() > 1000
                if loud and mic.t0 is not None:
                    phone.loud.append(time.monotonic() - mic.t0)
                if mic.ends and loud:
                    after = time.monotonic() - mic.ends[-1]
                    if not phone.audio_after or len(phone.audio_after) < len(mic.ends):
                        phone.audio_after.append(after)
        asyncio.get_running_loop().create_task(read())

    await pc.setLocalDescription(await pc.createOffer())
    conn = await voicertc.connect(pc.localDescription.sdp, pc.localDescription.type, allow_loopback=True)
    extra = {} if turning is None else {"turning": turning}      # the g6.* switches (default: as the flags' defaults)
    extra.update({k: v for k, v in (("speculate", speculate), ("discard", discard)) if v is not None})
    call = voicertc.build_call(conn, voice=voice, answer=answer, interrupt=interrupt, openers=openers, greeting=greeting,
                               warning=warning, gate=gate, **extra)
    run = asyncio.get_running_loop().create_task(call.run())
    ans = conn.get_answer()
    await pc.setRemoteDescription(RTCSessionDescription(sdp=ans["sdp"], type=ans["type"]))
    pinger = asyncio.get_running_loop().create_task(pings())
    scripted = asyncio.get_running_loop().create_task(script())
    try:
        deadline = time.monotonic() + timeout
        while not wait_for(phone) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
    finally:
        pinger.cancel()
        scripted.cancel()
        await call.stop()
        await pc.close()
        await asyncio.wait_for(asyncio.gather(run, return_exceptions=True), 10)
    phone.voice = voice
    phone.candidates = [ln for ln in ans["sdp"].splitlines() if ln.startswith("a=candidate:")]
    return phone


def _needs_pipecat():
    pytest.importorskip("pipecat")
    pytest.importorskip("aiortc")


def test_a_call_over_webrtc_hears_the_owner_and_answers_with_audio():
    _needs_pipecat()
    asked = []

    async def answer(text, language, number, speaker):
        asked.append((text, language, number))
        speaker.feed("Sure, ")
        await asyncio.sleep(0.05)
        speaker.feed("I'll remind you tomorrow morning.")
        speaker.finish()
        return {"route": "talk", "asked": False}

    voice = FakeVoice()
    openers = OpenerCache(lambda text, lang: (b"\x00\x10" * 2400, 24000))
    openers.warm({"en": ("Sure,",)})
    phone = asyncio.run(_call(answer, voice=voice, openers=openers))
    assert asked == [("Remind me to call the dentist tomorrow morning.", "en", 1)]
    assert phone.audio_after, f"no audio came back; events: {phone.events}"
    kinds = [e.get("type") for e in phone.events]
    assert "heard" in kinds and "transcript" in kinds and "timing" in kinds and "say" in kinds
    timing = next(e for e in phone.events if e.get("type") == "timing")
    srv = timing["server"]
    for key in ("vad_ms", "stt_ms", "turn_at_ms", "first_token_at_ms", "first_audio_at_ms"):
        assert key in srv, srv
    assert srv["cached_opener"] is True                 # "Sure," came from the cache: no voice made for it
    assert voice.spoken == ["I'll remind you tomorrow morning."]
    said = [e["text"] for e in phone.events if e.get("type") == "say"]
    assert said == ["Sure,", "I'll remind you tomorrow morning."]
    # the PC offered only its tailnet (here: loopback) address
    assert phone.candidates and all(" 127.0.0.1 " in c for c in phone.candidates), phone.candidates
    print("\nwebrtc loopback: end of speech -> audio at the phone %d ms; server %s"
          % (phone.audio_after[0] * 1000, srv))


def test_talking_over_kairo_stops_its_voice_and_the_answer():
    _needs_pipecat()
    stopped, turns = [], []

    async def answer(text, language, number, speaker):
        turns.append(number)
        if number == 1:
            for i in range(30):                              # a long answer: the owner talks over it
                speaker.feed(f"This is sentence number {i} of a long answer. ")
                await asyncio.sleep(0.02)
            speaker.finish()
            return {"route": "talk"}
        speaker.feed("Okay.")
        speaker.finish()
        return {"route": "talk"}

    voice = FakeVoice()
    voice.tone = _tone(1.5)                                  # every sentence plays for 1.5 s
    phone = asyncio.run(_call(answer, times=(0.5, 5.0), voice=voice, interrupt=stopped.append,
                              wait_for=lambda p: len(p.audio_after) >= 2 or (stopped and 2 in turns and len(p.audio_after) >= 2),
                              timeout=30, turning=voicertc.Turning(transcript_barge_in=False)))   # the old start: the VAD alone
    assert stopped == [1], (stopped, turns, phone.events)
    assert turns == [1, 2]
    # the long answer played until the owner talked (from 5.0 s), then not at all while they spoke
    assert any(1.0 < t < 5.0 for t in phone.loud)
    assert not [t for t in phone.loud if 5.8 < t < 7.4], [t for t in phone.loud if 5.8 < t < 7.4][:5]


# ---------------------------------------------------------------- "I'm done": a tap ends the turn

def _steady(seconds: float):
    """A loud steady sound (48 kHz) that never pauses: with _loud_is_speech, the owner in a loud room, where
    the VAD never hears the pause that would end the turn."""
    import numpy as np
    return (6000 * np.sin(2 * np.pi * 220 * np.arange(int(seconds * 48000)) / 48000)).astype(np.int16)


def _loud_is_speech(monkeypatch):
    """Silero hears every loud frame as speech, the way it hears a room full of people talking."""
    import numpy as np
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    monkeypatch.setattr(SileroVADAnalyzer, "voice_confidence",
                        lambda self, buf: 1.0 if np.abs(np.frombuffer(buf, np.int16)).max(initial=0) > 1000 else 0.0)


def test_a_tap_ends_the_turn_at_once_when_the_pc_never_hears_a_pause(monkeypatch):
    """Owner's request 2026-10-20: "when I'm around people I can just tap it and then it knows I'm done
    talking". The VAD hears speech for 12 s without a pause; the phone sends "done" at 3 s: the turn ends
    then (no pause, no Smart Turn), is answered, and is timed from the tap."""
    _needs_pipecat()
    _loud_is_speech(monkeypatch)
    asked = []

    async def answer(text, language, number, speaker):
        asked.append((text, number, time.monotonic()))
        speaker.finish("Okay.")
        return {"route": "talk"}

    voice = FakeVoice()
    phone = asyncio.run(_call(answer, voice=voice, speech=_steady(14.0), later=((3.0, {"type": "done"}, "speech"),),
                              wait_for=lambda p: bool(asked), timeout=10))
    assert [(text, n) for text, n, _ in asked] == [(voice.text, 1)], phone.events
    assert not phone.mic.ends or asked[0][2] < phone.mic.ends[0], "answered while the sound still goes on"
    assert asked[0][2] - phone.sent[0][0] < 2.5, "answered right after the tap"
    assert voice.heard == 1                                  # written down once, at the tap
    transcript = next(e for e in phone.events if e.get("type") == "transcript")
    assert transcript["ended"] == "tap"
    assert transcript["server"]["vad_ms"] == 0               # no pause waited for: timed from the tap
    kinds = [e.get("type") for e in phone.events]
    assert kinds.index("speech") < kinds.index("transcript")  # the phone knew a turn was going on


def test_a_tapped_turn_still_goes_through_the_speaker_check(monkeypatch):
    """The tap skips the wait, not the checks: not the owner (or too far away) = no answer."""
    _needs_pipecat()
    _loud_is_speech(monkeypatch)
    asked, checked = [], []

    async def answer(text, language, number, speaker):
        asked.append(text)
        speaker.finish("Okay.")
        return {"route": "talk"}

    async def gate():
        checked.append(time.monotonic())
        return False

    phone = asyncio.run(_call(answer, speech=_steady(14.0), later=((3.0, {"type": "done"}, "speech"),), gate=gate,
                              wait_for=lambda p: bool(checked), timeout=10))
    assert len(checked) == 1 and checked[0] - phone.sent[0][0] < 2.5, (checked, phone.events)
    assert asked == []
    assert not [e for e in phone.events if e.get("type") == "transcript"]


def test_a_tap_with_nothing_said_does_nothing_and_the_call_listens_on(monkeypatch):
    """"done" with no speech since the last turn is ignored (on the phone the tap pauses then); the next
    turn is heard and ended the usual way (the pause)."""
    _needs_pipecat()
    _loud_is_speech(monkeypatch)
    asked = []

    async def answer(text, language, number, speaker):
        asked.append((text, number, time.monotonic()))
        speaker.finish("Okay.")
        return {"route": "talk"}

    voice = FakeVoice()
    phone = asyncio.run(_call(answer, voice=voice, speech=_steady(1.5), times=(3.0,),
                              later=((1.0, {"type": "done"}),), wait_for=lambda p: bool(asked), timeout=10))
    assert voice.heard == 1 and [(text, n) for text, n, _ in asked] == [(voice.text, 1)], phone.events
    assert asked[0][2] - phone.mic.ends[0] > 0.1             # ended by the pause after the speech, not by the tap
    transcript = next(e for e in phone.events if e.get("type") == "transcript")
    assert transcript.get("ended") != "tap"                   # (g6: "silence" if Smart Turn said not done and the quiet ended it)


# ---------------------------------------------------------------- turn-taking (group G6 of the voice plan, section 1)
# Who may interrupt Kairo, when a turn ends, what a tap does. The fake phone, Silero and Smart Turn are the real ones;
# Whisper and the voices are fakes. Every switch (voicertc.Turning, flags g6.*) is tested with both values. They live in
# this file because CI's two voice jobs list it (a new file would need an edit of ci.yml, which the owner merges by hand).
# Timings are the sandbox's CPU (a shared 4-core machine), not the PC: the windows are wide on purpose.
#
# Defaults (flags.py, pinned by test_flags_g6.py): the two fixes (empty turn, tap) are on; the owner's three decisions (the
# 3 s Smart Turn wait, the ignore windows, barge-in only on words) and the long-speech cut are off until he switches them on,
# so Turning() is the call he has with nothing changed. A test of what one of those does says so with _on().

def _on(**changes) -> Turning:
    """The owner's three turn-taking decisions switched on (what he gets after turning them on in Experiments), then
    `changes` on top."""
    return Turning(**{"smart_turn_3s": True, "ignore_windows": True, "transcript_barge_in": True, **changes})


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

def test_the_app_reads_the_g6_switches_at_the_start_of_every_call(local, monkeypatch):
    _needs_pipecat()
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


@pytest.mark.parametrize("turning", [Turning(), _on()], ids=["defaults", "the_three_decisions_on"])
def test_a_tap_that_gets_ahead_of_the_words_still_ends_the_turn(turning):
    """The flake: under load (load 7 on 4 cores) the tap ended the turn at once in about 3 of 4 runs, and in the rest it
    was thrown away as 'nothing was said' and the answer came 7 s later. Cause: the tap reaches the turn detector while
    Whisper is through (nothing pending) but its transcript has not arrived yet, and the detector took 'no transcript
    yet' for 'no words'. Here the tap is held up 0.3 s and the words 0.8 s on purpose, so it happens every time."""
    assert turning.tap_lag_fix                                   # (a default: the fix is on)
    r = _tapped(turning, words_late_s=0.8, tap_late_s=0.3)
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
    32 ms of audio). With the fix the speech ends where the owner tapped: Whisper gets the whole 2.5 s (3.1 s of audio, with
    the second before the VAD heard speech) and the answer follows the tap by about 1.5 s. The old code let the VAD go deaf
    at the moment of the tap, wherever it had got to (2.2 s of audio here), and, being behind, lost the tap itself: its
    answer came only at the hang-up."""
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


def _unfinished_thought(turning: Turning, *, silence_s: float = 2.0, watchdog_s: float = 2.5):
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
    assert on_wait > 1.9, (on_wait, off_wait)                             # 0.2 s VAD + 2.0 s + the answer's start
    assert off_wait < 2.0 and on_wait - off_wait > 1.0, (on_wait, off_wait)   # the old 0.5 s, a full second sooner
    assert on_event.get("ended") == "silence" and off_event.get("ended") == "silence"   # no tap, no watchdog: the quiet ended it


def test_a_turn_with_no_words_ends_at_the_watchdog_and_the_switch_picks_which():
    """A sound Whisper writes nothing for opens a turn nothing can end except the watchdog: 3.5 s with the switch on
    (Pipecat's own 5.0 s off; shortened here to 1.0 s and 3.5 s). The call says so in its log."""
    _needs_pipecat()
    ends = {}
    for on in (True, False):
        voice = ScriptedVoice("")
        with _Stamped() as seen, pytest.MonkeyPatch.context() as mp:
            mp.setattr(voicertc, "TURN_STOP_TIMEOUT_S", 1.0)
            mp.setattr(voicertc, "TURN_STOP_TIMEOUT_OLD_S", 3.5)
            phone = asyncio.run(_call(lambda *a: None, voice=voice, turning=Turning(smart_turn_3s=on),
                                      wait_for=_after(10.0), timeout=16))
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


def _over_kairo(over_says: str, turning: Turning | None = None, *, after_s: float = 3.0, clip_s: float = 0.8,
                lines=KAIRO_SAYS, seconds: float = 10.0, **call):
    """The owner asks something, Kairo starts a long answer (every line a 3 s tone), and the owner makes a sound over it
    that Whisper writes down as `over_says`. Returns what the call did."""
    _needs_pipecat()
    turning = _on() if turning is None else turning               # barge-in only on words, unless a test says otherwise
    asked, stopped = [], []
    voice = ScriptedVoice("Remind me to call the dentist tomorrow morning.", over_says)
    voice.tone = _tone(3.0)
    interrupt = call.pop("interrupt", stopped.append)
    phone = asyncio.run(_call(_kairo_lines(asked, lines), voice=voice, interrupt=interrupt,
                              turning=turning, speech=_speech_then(gap_s=after_s, clip_s=clip_s), wait_for=_after(seconds),
                              timeout=seconds + 8, **call))
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


def test_the_words_only_start_works_without_the_other_two_switches():
    """Each switch is its own: barge-in on words alone (no ignore window, no 3 s wait) lets 'Mhm.' go by and obeys 'Stop.'."""
    alone = Turning(transcript_barge_in=True)
    mhm = _over_kairo("Mhm.", alone)
    assert mhm.stopped == [] and "barge_in" not in mhm.kinds, (mhm.stopped, mhm.kinds)
    stop = _over_kairo("Stop.", alone)
    assert stop.stopped == [1] and stop.kinds.count("barge_in") == 1, (stop.stopped, stop.kinds)


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


def test_with_the_switches_off_any_sound_over_kairo_stops_it_the_old_way_and_the_phone_is_still_told():
    """The owner switched the three turn-taking decisions off (flags.py; they are on by default): the call stops at the
    VAD's start, as before G6."""
    r = _over_kairo("Mhm.", Turning(smart_turn_3s=False, ignore_windows=False, transcript_barge_in=False))
    assert r.stopped == [1], (r.stopped, r.kinds)                  # the VAD's start alone, as before
    assert r.kinds.count("barge_in") == 1                          # g7.listen_on_barge_in relies on the PC saying so either way
    assert r.kinds.count("speech") == 2                            # and every stretch of speech told the phone


def test_a_barge_in_is_told_to_the_phone_once_and_only_when_kairos_voice_was_playing():
    r = _over_kairo("Stop.")
    assert r.kinds.count("barge_in") == 1
    quiet = _over_kairo("Stop.", after_s=0.0, lines=[])      # nothing for Kairo to say: the words are just a turn
    assert "barge_in" not in quiet.kinds and quiet.stopped in ([], [1])


def test_a_backchannel_over_kairo_leaves_nothing_behind_that_stops_the_next_real_interruption():
    """'Mhm' over Kairo opens a turn nothing will answer; it closes (or is still waiting for the quiet) when the owner
    really breaks in a few seconds later, and that is heard and obeyed all the same."""
    _needs_pipecat()
    import numpy as np
    speech = _speech_48k()
    gap = np.zeros(int(2.0 * 48000), dtype=np.int16)
    track = np.concatenate([speech, np.zeros(int(2.5 * 48000), dtype=np.int16), speech[:int(0.7 * 48000)], gap,
                            speech[:int(0.9 * 48000)]])
    asked, stopped = [], []
    voice = ScriptedVoice("Remind me to call the dentist tomorrow morning.", "Mhm.", "No wait, Tuesday.")
    voice.tone = _tone(3.0)
    phone = asyncio.run(_call(_kairo_lines(asked, KAIRO_SAYS), voice=voice, interrupt=stopped.append, speech=track,
                              turning=_on(), wait_for=lambda p: bool(stopped) and len(asked) >= 2, timeout=20))
    kinds = [e.get("type") for e in phone.events]
    assert stopped == [1] and kinds.count("barge_in") == 1, (stopped, kinds)
    assert [t for t, _ in asked] == ["Remind me to call the dentist tomorrow morning.", "No wait, Tuesday."], asked


def test_the_voice_model_starts_early_on_the_words_that_stopped_kairo_only_after_kairo_was_stopped():
    """The voice model starts on the owner's words at their pause, before Smart Turn has decided. For words that stop Kairo
    the interruption has to come first: it cancels the answer in progress, and a start made before it would find the old
    answer's task still alive and not start at all."""
    order = []
    r = _over_kairo("No wait, Tuesday.", interrupt=lambda n: order.append(("interrupt", n)),
                    speculate=lambda words, lang: order.append(("speculate", words)))
    assert r.stopped == [] and ("interrupt", 1) in order, order       # (our own interrupt callback, not _over_kairo's list)
    assert order.index(("interrupt", 1)) < order.index(("speculate", "No wait, Tuesday.")), order
    assert order.count(("speculate", "No wait, Tuesday.")) == 1, order
    assert ("speculate", "Remind me to call the dentist tomorrow morning.") in order      # and the first turn's, as before


def test_nothing_is_started_early_on_a_sound_that_did_not_stop_kairo():
    order = []
    _over_kairo("Mhm.", interrupt=lambda n: order.append(("interrupt", n)),
                speculate=lambda words, lang: order.append(("speculate", words)))
    assert order == [("speculate", "Remind me to call the dentist tomorrow morning.")], order


# ---------------------------------------------------------------- a long talk-over stops Kairo without waiting for a pause (g6.long_speech_cut)

def test_a_long_stretch_of_speech_over_kairo_stops_it_before_the_owner_pauses_with_the_switch_on():
    """Off by default. The owner talks over Kairo for 2.5 s without a pause: Whisper can only write it down at the pause,
    so without the switch Kairo talks on for the whole stretch (the research: speech length + 0.19 s + Whisper). With it, a
    stretch that has gone on for LONG_SPEECH_S (1.0 s) after the VAD heard it start stops Kairo: the delay is capped at about
    1.3 s. Whisper writes the stretch down as 'Mhm.' here, so only the switch can have stopped Kairo."""
    on = _over_kairo("Mhm.", _on(long_speech_cut=True), clip_s=2.5)
    off = _over_kairo("Mhm.", _on(long_speech_cut=False), clip_s=2.5)
    assert off.stopped == [] and "barge_in" not in off.kinds, (off.stopped, off.kinds)
    assert on.stopped == [1] and on.kinds.count("barge_in") == 1, (on.stopped, on.kinds)
    speech_start = 0.5 + 2.54 + 3.0                                  # where the owner's long sound begins on the phone's clock
    assert on.at["barge_in"] < speech_start + 2.4, on.at              # before the sound even ended (it lasts 2.5 s)
    assert on.at["barge_in"] > speech_start + 0.9, on.at              # and not before LONG_SPEECH_S


def test_the_long_speech_cut_ignores_a_short_sound_and_speech_while_kairo_is_quiet():
    short = _over_kairo("Mhm.", _on(long_speech_cut=True), clip_s=0.6)
    assert short.stopped == [] and "barge_in" not in short.kinds       # 0.6 s is not a talk-over
    quiet = _over_kairo("Remind me later.", _on(long_speech_cut=True), after_s=0.0, lines=[], clip_s=2.5)
    assert "barge_in" not in quiet.kinds                               # nothing was speaking: just the next turn


# ---------------------------------------------------------------- through the app (/api/rtc/offer)

from tests.test_voice_local import local  # noqa: E402,F401  (the app with the fake voice model and Claude)


async def _app_call(client, *, path="call", seconds=12.0):
    """The fake phone calls the app: offer over HTTP, audio over WebRTC on this machine."""
    import numpy as np
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

    voicertc.tailnet_only(allow_loopback=True)
    phone = SimpleNamespace(events=[], audio_after=[])
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))   # like the phone: no STUN/TURN (aiortc's default asks Google)
    mic = _phone_track([0.5])
    pc.addTrack(mic)
    channel = pc.createDataChannel("kairo")
    channel.on("message", lambda m: phone.events.append(json.loads(m)) if m != "pong" and m[:1] == "{" else None)

    @pc.on("track")
    def on_track(track):
        async def read():
            while True:
                try:
                    f = await track.recv()
                except Exception:
                    return
                if mic.ends and not phone.audio_after and np.abs(f.to_ndarray()).max() > 1000:
                    phone.audio_after.append(time.monotonic() - mic.ends[-1])
        asyncio.get_running_loop().create_task(read())

    await pc.setLocalDescription(await pc.createOffer())
    r = await asyncio.to_thread(client.post, "/api/rtc/offer",
                                json={"sdp": pc.localDescription.sdp, "type": "offer", "path": path})
    assert r.status_code == 200, r.text
    await pc.setRemoteDescription(RTCSessionDescription(sdp=r.json()["sdp"], type=r.json()["type"]))
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not any(e.get("type") == "done" for e in phone.events):
        if channel.readyState == "open":
            channel.send("ping")
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.5)
    await pc.close()
    return phone


class ToneVoice:
    """The app's fake voice, but its lines are audible (a tone) so the phone can hear them."""

    def __init__(self, inner):
        self.inner, self.tone = inner, _tone(0.3)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def speak(self, text, language=None):
        self.inner.spoken.append((text, language))
        return self.tone, language or "en"


def test_a_call_through_the_app_is_answered_by_the_voice_model(local, monkeypatch):
    _needs_pipecat()
    monkeypatch.setattr(voicertc, "ALLOW_LOOPBACK", True)
    local.voice.text = "hey how are you"
    local.app.state.voice = ToneVoice(local.voice)
    local.app.state.openers = OpenerCache(lambda text, lang: (b"\x00\x10" * 2400, 24000))   # no warm-up noise
    phone = asyncio.run(_app_call(local))
    kinds = [e.get("type") for e in phone.events]
    assert "transcript" in kinds and "timing" in kinds, kinds
    done = next(e for e in phone.events if e.get("type") == "done")
    assert done["model"] == "lfm2.5-2.6b" and done["text"] == "Sure, happy to help with that."
    assert phone.audio_after, phone.events
    assert ("happy to help with that.", "en") in local.voice.spoken
    from tests.test_voice_local import requests_seen
    turns_asked = [r for r in requests_seen(local) if r.get("max_tokens") != 1]      # not the call's priming
    assert len(turns_asked) == 1, turns_asked
    assert done["early"] is True                             # started at the pause, used for the turn
    msgs = [(m["role"], m["text"]) for m in local.get("/api/chat/messages").json()["messages"]]
    assert ("owner", "(spoken, on a call) hey how are you") in msgs and ("kairo", "Sure, happy to help with that.") in msgs
    deadline = time.monotonic() + 5
    while local.app.state.today_card.on_call and time.monotonic() < deadline:   # hung up: the line is free again
        time.sleep(0.1)
    assert not local.app.state.today_card.on_call


def test_without_a_tailnet_address_the_phone_is_told_at_once(local, monkeypatch):
    _needs_pipecat()
    import aioice.ice
    monkeypatch.setattr(aioice.ice, "get_host_addresses", lambda use_ipv4, use_ipv6: ["192.168.1.20"])
    assert local.get("/api/rtc").json() == {"available": False, "reason": "no_tailnet"}
    monkeypatch.setattr(aioice.ice, "get_host_addresses", lambda use_ipv4, use_ipv6: ["192.168.1.20", "100.70.1.2"])
    assert voicertc.tailnet_addresses() == ["100.70.1.2"]
    assert local.get("/api/rtc").json()["available"] is True


def test_without_the_voice_packages_the_phone_is_told_to_use_the_old_line(local, monkeypatch):
    monkeypatch.setattr(voicertc, "available", lambda: False)
    assert local.get("/api/rtc").json() == {"available": False, "reason": "not_installed"}
    r = local.post("/api/rtc/offer", json={"sdp": "v=0\r\n", "type": "offer"})
    assert r.status_code == 409 and r.json()["detail"] == "not_installed"
    assert local.post("/api/rtc/offer", json={"type": "offer"}).status_code == 400


def test_a_call_the_owner_places_opens_with_the_greeting_from_the_cache():
    """Owner's request 2026-10-17: "Hey <first name>." at once: no voice made for it, no model."""
    _needs_pipecat()

    async def answer(text, language, number, speaker):
        speaker.finish("Okay.")
        return {"route": "talk"}

    voice = FakeVoice()
    openers = OpenerCache(lambda text, lang: (b"\x00\x10" * 4800, 24000))
    openers.warm({"en": ("Hey Sam.",)})
    phone = asyncio.run(_call(answer, times=(30.0,), voice=voice, openers=openers, greeting="Hey Sam.",
                              first_message={"type": "greet"},
                              wait_for=lambda p: any(e.get("type") == "say" for e in p.events), timeout=15))
    said = [e["text"] for e in phone.events if e.get("type") == "say"]
    assert said == ["Hey Sam."] and voice.spoken == []          # from the cache: nothing synthesized


def test_a_slow_voice_is_said_once_right_after_the_greeting():
    """Owner's decision 2026-10-17: a voice off the GPU never blocks the call; Kairo says so once, at the
    start, after the greeting (made at start like the other short lines, so it plays at once too)."""
    _needs_pipecat()
    from app.voicelink import SLOW_LINE

    async def answer(text, language, number, speaker):
        speaker.finish("Okay.")
        return {"route": "talk"}

    voice = FakeVoice()
    openers = OpenerCache(lambda text, lang: (b"\x00\x10" * 4800, 24000))
    openers.warm({"en": ("Hey Sam.", SLOW_LINE)})
    phone = asyncio.run(_call(answer, times=(30.0,), voice=voice, openers=openers, greeting="Hey Sam.",
                              first_message={"type": "greet"}, warning=SLOW_LINE,
                              wait_for=lambda p: sum(e.get("type") == "say" for e in p.events) >= 2, timeout=15))
    said = [e["text"] for e in phone.events if e.get("type") == "say"]
    assert said == ["Hey Sam.", SLOW_LINE] and voice.spoken == []


# ---------------------------------------------------------------- the uplink jitter buffer (group G4, app/rtcjitter.py)
# The checks that need the real library for the patch behind the owner's flag g4.jitter_patch (the ones with stand-in
# parts are in tests/test_rtcjitter.py). They are in this file because the two voice CI jobs (Linux and Windows), the only
# ones with aiortc and Pipecat installed, already run it; a file of their own would need an edit of .github/workflows/ci.yml,
# and a pull request that touches .github/ is merged by the owner by hand (CONTRIBUTING.md). A test in test_rtcjitter.py
# fails when a test file that skips without the voice packages is not in both jobs.

JITTER_FLAG = "g4.jitter_patch"



def _real_pc():
    """A peer connection with an audio and a video receiver, like the library builds them for a call."""
    from aiortc import RTCConfiguration, RTCPeerConnection
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))      # no STUN, like Kairo and the phone
    pc.addTransceiver("audio")
    pc.addTransceiver("video")
    return pc


def _real_buffers(pc) -> dict:
    return {t.kind: getattr(t.receiver, rtcjitter.SLOT) for t in pc.getTransceivers()}


def test_the_real_library_has_what_the_patch_touches_and_the_pin_is_installed():
    """Fails on a version bump or a rename inside the library: then the patch (and its numbers) must be checked again."""
    pytest.importorskip("aiortc")

    async def go():
        pc = _real_pc()
        try:
            stock = _real_buffers(pc)
            assert (stock["audio"]._capacity, stock["audio"]._prefetch, stock["audio"]._is_video) == (16, 4, False)
            assert (stock["video"]._capacity, stock["video"]._is_video) == (128, True)
            assert stock["audio"]._origin is None and len(stock["audio"]._packets) == 16
            name, version = rtcjitter._library_version(pc)
            assert (name, version in rtcjitter.SUPPORTED) == ("aiortc", True), (name, version)
            assert rtcjitter.install(pc) is True
            now = _real_buffers(pc)
            assert (now["audio"]._capacity, now["audio"]._prefetch) == (8, 1) and now["audio"] is not stock["audio"]
            assert now["video"] is stock["video"]
        finally:
            await pc.close()
    asyncio.run(go())


def _lag(buffer, lost_at: int | None = None, n: int = 400) -> list[int]:
    """Feed packets 20 ms apart (one frame each, sequence +1, timestamp +960), skip number lost_at, and record
    after every packet: packets in front of the newest one handed on. The buffer is the library's own."""
    from aiortc.rtp import RtpPacket
    lag, newest = [], None
    for i in range(n):
        if i == lost_at:
            continue
        p = RtpPacket.parse(RtpPacket(payload_type=96, sequence_number=(500 + i) % 65536,
                                      timestamp=1000 + 960 * i, payload=b"x" * 20).serialize())
        p._data = p.payload                                      # what the receiver does after depayloading
        _, frame = buffer.add(p)
        if frame is not None:
            newest = (frame.timestamp - 1000) // 960
        if newest is not None:
            lag.append(i - newest)
    return lag


def test_the_buffers_delay_in_packets_clean_and_after_one_lost_packet():
    """Exact (arithmetic on 20 ms packets, no clock): the library's 16/4 holds the voice back 4 packets = 80 ms,
    and after ONE lost packet 14 packets = 280 ms for the rest of the call. 8/1: 1 packet = 20 ms, then 6 = 120 ms."""
    pytest.importorskip("aiortc")

    async def buffers():
        pc = _real_pc()
        try:
            stock = _real_buffers(pc)["audio"]
            assert rtcjitter.install(pc) is True
            return stock, _real_buffers(pc)["audio"]
        finally:
            await pc.close()
    stock, patched = asyncio.run(buffers())
    from aiortc.jitterbuffer import JitterBuffer as Library
    stock_again = Library(capacity=16, prefetch=4)               # a second, untouched 16/4 for the lossy run
    patched_again = Library(capacity=8, prefetch=1)
    clean, lossy = _lag(stock), _lag(stock_again, lost_at=100)
    assert clean[50] == 4 and clean[-1] == 4                     # 80 ms, steady
    assert max(lossy[95:120]) == 19 and lossy[-1] == 14          # 380 ms just after the loss, 280 ms from then on
    clean, lossy = _lag(patched), _lag(patched_again, lost_at=100)
    assert clean[50] == 1 and clean[-1] == 1                     # 20 ms
    assert max(lossy[95:120]) == 8 and lossy[-1] == 6            # 160 ms just after the loss, 120 ms from then on


# in a call: voicertc.connect and the app's flag

def _shape_of(conn) -> list[tuple[int, int]]:
    """(capacity, prefetch) of each audio receiver's buffer on a call's connection."""
    return [(t.receiver.__dict__[rtcjitter.SLOT]._capacity, t.receiver.__dict__[rtcjitter.SLOT]._prefetch)
            for t in conn.pc.getTransceivers() if t.kind == "audio"]


async def _phone_offer():
    """The fake phone: a peer connection with a microphone, and its offer."""
    from aiortc import RTCConfiguration, RTCPeerConnection

    voicertc.tailnet_only(allow_loopback=True)
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    pc.addTrack(_phone_track([0.5]))
    await pc.setLocalDescription(await pc.createOffer())
    return pc


@pytest.mark.parametrize("patch, shape", [(False, (16, 4)), (True, (8, 1))])
def test_a_call_connection_has_the_8_and_1_buffer_only_when_asked(patch, shape):
    """The receivers exist when connect() returns (before build_call), so that is where it has to happen."""
    _needs_pipecat()

    async def go():
        pc = await _phone_offer()
        conn = await voicertc.connect(pc.localDescription.sdp, pc.localDescription.type, allow_loopback=True,
                                      jitter_patch=patch)
        try:
            return _shape_of(conn)
        finally:
            await conn.disconnect()
            await pc.close()
    assert asyncio.run(go()) == [shape]


def test_connect_without_the_argument_leaves_the_library_as_it_is():
    _needs_pipecat()

    async def go():
        pc = await _phone_offer()
        conn = await voicertc.connect(pc.localDescription.sdp, pc.localDescription.type, allow_loopback=True)
        try:
            return _shape_of(conn)
        finally:
            await conn.disconnect()
            await pc.close()
    assert asyncio.run(go()) == [(16, 4)]


def test_the_app_reads_the_flag_at_the_start_of_every_call(local, monkeypatch):
    """Off by default; the owner turns it on and the next call has the small buffer; off again, the next is stock."""
    _needs_pipecat()
    from aiortc import RTCSessionDescription
    monkeypatch.setattr(voicertc, "ALLOW_LOOPBACK", True)
    seen = []
    real = voicertc.connect

    async def spy(*args, **kwargs):
        conn = await real(*args, **kwargs)
        seen.append(_shape_of(conn))
        return conn
    monkeypatch.setattr(voicertc, "connect", spy)

    async def call():
        pc = await _phone_offer()
        r = await asyncio.to_thread(local.post, "/api/rtc/offer",
                                    json={"sdp": pc.localDescription.sdp, "type": "offer", "path": "call"})
        assert r.status_code == 200, r.text
        await pc.setRemoteDescription(RTCSessionDescription(sdp=r.json()["sdp"], type=r.json()["type"]))
        await pc.close()

    async def go():
        await call()                                                    # nothing set: the default
        assert (await asyncio.to_thread(local.put, "/api/flags/" + JITTER_FLAG, json={"on": True})).status_code == 200
        await call()
        assert (await asyncio.to_thread(local.put, "/api/flags/" + JITTER_FLAG, json={"on": False})).status_code == 200
        await call()
    asyncio.run(go())
    assert seen == [[(16, 4)], [(8, 1)], [(16, 4)]]


def test_a_connection_without_the_peer_connection_it_expects_still_makes_the_call(caplog, monkeypatch):
    """The patch is an extra: whatever the pipecat connection looks like one day, connect() returns it."""
    _needs_pipecat()
    from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection


    def gone(self):
        raise AttributeError("no pc any more")
    monkeypatch.setattr(SmallWebRTCConnection, "pc", property(gone))

    async def go():
        pc = await _phone_offer()
        with caplog.at_level(logging.INFO, logger="kairo.rtcjitter"):
            conn = await voicertc.connect(pc.localDescription.sdp, pc.localDescription.type, allow_loopback=True,
                                          jitter_patch=True)
        await conn.disconnect()
        await pc.close()
        return conn
    assert asyncio.run(go()) is not None
    assert "left as it is" in caplog.text


# The jitter buffer's real cost, measured on a real RTP path (voice plan section 7, E1; section 11, S3). A fake phone
# (aiortc, in this process) sends 100 ms tone bursts at a random moment from a microphone paced in real time; Kairo's
# side is voicertc.connect() as in a call; the delay is from the moment the phone hands the first frame of a burst to
# its encoder until the PC's receiver delivers the decoded frame (RemoteStreamTrack.recv()). Opus, packetising,
# DTLS-SRTP, UDP on 127.0.0.1 and the decoder are all in it, the network is not. Then ONE packet is lost on the way,
# and the bursts after it are timed again. The numbers are the sandbox's CPU (a shared 4-core machine, 82 / 282 ms
# stock, 22 / 121 ms patched), not the PC. The windows are wide on purpose (the timers of Windows tick every 15 ms, a
# busy machine adds more); the two buffers are also compared with each other, from the same run.

UPLINK_BURST_FRAMES = 5                       # 100 ms of tone
UPLINK_WARM_UP_S = 1.5
UPLINK_SETTLE_S = 1.2                         # after the loss: the stock buffer fills up (16 packets = 320 ms) and gives up on the gap


def _uplink_mic():
    import numpy as np
    from aiortc import MediaStreamTrack
    from av import AudioFrame
    tone = (6000 * np.sin(2 * np.pi * 1000 * np.arange(960) / 48000)).astype(np.int16)

    class Mic(MediaStreamTrack):
        """48 kHz mono, 20 ms frames, handed over at real-time pace; burst() makes the next 5 frames a tone."""
        kind = "audio"

        def __init__(self):
            super().__init__()
            self.t0, self.n, self.tone_left, self.onsets = None, 0, 0, []

        def burst(self):
            self.tone_left = UPLINK_BURST_FRAMES
            self._first = True

        async def recv(self):
            if self.t0 is None:
                self.t0 = time.perf_counter()
            self.n += 1
            await asyncio.sleep(max(0.0, self.t0 + self.n * 0.02 - time.perf_counter()))
            if self.tone_left:
                if self.tone_left == UPLINK_BURST_FRAMES:
                    self.onsets.append(time.perf_counter())             # the first tone frame goes to the encoder now
                chunk, self.tone_left = tone, self.tone_left - 1
            else:
                chunk = np.zeros(960, dtype=np.int16)
            f = AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
            f.sample_rate, f.pts, f.time_base = 48000, self.n * 960, fractions.Fraction(1, 48000)
            return f
    return Mic()


async def _uplink_measure(patch: bool, bursts: int, lose_one: bool, seed: int = 3) -> dict:
    """Delay (ms) of each burst, before and (with lose_one) after one lost uplink packet."""
    import numpy as np
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    from aiortc.rtcdtlstransport import RTCDtlsTransport
    rng = random.Random(seed)
    voicertc.tailnet_only(allow_loopback=True)
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    mic = _uplink_mic()
    pc.addTrack(mic)
    await pc.setLocalDescription(await pc.createOffer())
    conn = await voicertc.connect(pc.localDescription.sdp, pc.localDescription.type, allow_loopback=True,
                                  jitter_patch=patch)
    answer = conn.get_answer()
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))

    arrivals: list[float] = []
    quiet = True

    async def listen(track):
        nonlocal quiet
        while True:
            try:
                f = await track.recv()
            except Exception:
                return
            x = f.to_ndarray().astype(np.float64)
            level = 20 * np.log10(max(np.sqrt(np.mean(x * x)) / 32768.0, 1e-9))
            if quiet and level > -30:
                arrivals.append(time.perf_counter())
                quiet = False
            elif level < -45:
                quiet = True
    track = next(t.receiver.track for t in conn.pc.getTransceivers() if t.kind == "audio")
    reader = asyncio.get_running_loop().create_task(listen(track))

    lost = {"arm": False, "n": 0, "transport": None}
    send = RTCDtlsTransport._send_rtp

    async def lossy(self, data):
        if self is lost["transport"] and lost["arm"] and not 192 <= data[1] <= 223:     # not RTCP
            lost["arm"], lost["n"] = False, lost["n"] + 1
            return None                                                                # gone on the way to the PC
        return await send(self, data)
    RTCDtlsTransport._send_rtp = lossy
    try:
        deadline = time.perf_counter() + 15
        while not (pc.connectionState == conn.pc.connectionState == "connected") and time.perf_counter() < deadline:
            await asyncio.sleep(0.05)
        assert pc.connectionState == conn.pc.connectionState == "connected"
        lost["transport"] = pc.getSenders()[0].transport
        await asyncio.sleep(UPLINK_WARM_UP_S)

        async def run_bursts(n: int) -> list[float]:
            first = len(mic.onsets)
            for _ in range(n):
                await asyncio.sleep(rng.uniform(0.45, 0.6))
                mic.burst()
            await asyncio.sleep(0.6)
            delays, j = [], 0
            for t in mic.onsets[first:]:
                while j < len(arrivals) and arrivals[j] < t:
                    j += 1
                if j < len(arrivals) and arrivals[j] - t < 1.0:
                    delays.append((arrivals[j] - t) * 1000)
                    j += 1
            return delays
        out = {"clean": await run_bursts(bursts)}
        if lose_one:
            lost["arm"] = True
            while lost["n"] == 0:
                await asyncio.sleep(0.01)
            await asyncio.sleep(UPLINK_SETTLE_S)
            out["after_loss"] = await run_bursts(bursts)
        out["lost"] = lost["n"]
        out["bursts"] = bursts
        return out
    finally:
        RTCDtlsTransport._send_rtp = send
        reader.cancel()
        await conn.disconnect()
        await pc.close()


def _p50(xs: list[float]) -> float:
    return statistics.median(xs)


def _uplink_run(patch: bool, lose_one: bool = True, bursts: int = 8) -> dict:
    out = asyncio.run(_uplink_measure(patch, bursts, lose_one))
    label = "8/1 (patched)" if patch else "16/4 (stock)"
    for key in ("clean", "after_loss"):
        if key in out:
            xs = sorted(out[key])
            print(f"\nuplink jitter buffer {label}, {key}: p50 {_p50(xs):.1f} ms, min {xs[0]:.1f}, max {xs[-1]:.1f}, "
                  f"{len(xs)}/{out['bursts']} bursts heard")
    return out


@pytest.fixture(scope="module")
def uplink_runs():
    """One call per setting (about 12 s each), both under the same load; the tests below read them."""
    _needs_pipecat()
    return {"stock": _uplink_run(patch=False), "patched": _uplink_run(patch=True)}


def test_the_stock_buffer_delays_every_turn_80_ms_and_one_lost_packet_makes_it_280(uplink_runs):
    out = uplink_runs["stock"]
    assert out["lost"] == 1 and len(out["clean"]) >= 6 and len(out["after_loss"]) >= 6
    assert 50 < _p50(out["clean"]) < 140, out["clean"]            # E1: 82.5 ms = 4 packets of 20 ms + about 2.5 ms
    assert 220 < _p50(out["after_loss"]) < 400, out["after_loss"]  # E1: 280 ms for the rest of the call


def test_the_patched_buffer_delays_20_ms_and_after_a_lost_packet_120(uplink_runs):
    out = uplink_runs["patched"]
    assert out["lost"] == 1 and len(out["clean"]) >= 6 and len(out["after_loss"]) >= 6
    assert 5 < _p50(out["clean"]) < 60, out["clean"]               # E1: 22.3 ms
    assert 80 < _p50(out["after_loss"]) < 210, out["after_loss"]   # E1: 120-144 ms with the buffer's 6 packets


def test_the_patch_takes_60_ms_off_every_turn_and_160_ms_after_a_loss(uplink_runs):
    """Both runs saw the same machine at the same time, so this holds when the absolute numbers drift."""
    saved_clean = _p50(uplink_runs["stock"]["clean"]) - _p50(uplink_runs["patched"]["clean"])
    saved_after_loss = _p50(uplink_runs["stock"]["after_loss"]) - _p50(uplink_runs["patched"]["after_loss"])
    print(f"\nsaved per turn: {saved_clean:.1f} ms; after one lost packet: {saved_after_loss:.1f} ms")
    assert 25 < saved_clean < 95                                   # 3 packets of 20 ms
    assert 90 < saved_after_loss < 240                            # 8 packets of 20 ms


def test_the_app_hands_a_call_the_model_the_switch_says(local, monkeypatch, tmp_path):
    """G3 (g3.smart_turn_pin; here so both voice CI jobs run it): /api/rtc/offer reads the switch at the start of each call and gives build_call the file (or None: pipecat's copy)."""
    _needs_pipecat()
    from app import voicellm
    pinned = tmp_path / "smart-turn-v3.2-cpu.onnx"
    monkeypatch.setattr(voicellm, "smart_turn_pinned_path", lambda site: pinned)
    monkeypatch.setattr(voicertc, "ALLOW_LOOPBACK", True)
    local.app.state.voice = ToneVoice(local.voice)
    seen = []
    original = voicertc.build_call

    def spy(*a, **k):
        seen.append(k.get("smart_turn_model"))
        return original(*a, **{**k, "smart_turn_model": None})        # the stand-in file is not a model: load pipecat's own
    monkeypatch.setattr(voicertc, "build_call", spy)
    asyncio.run(_app_call(local, seconds=2))
    assert local.put("/api/flags/g3.smart_turn_pin", json={"on": True}).status_code == 200
    asyncio.run(_app_call(local, seconds=2))
    assert local.put("/api/flags/g3.smart_turn_pin", json={"on": False}).status_code == 200
    asyncio.run(_app_call(local, seconds=2))
    assert seen == [None, str(pinned), None]

def test_g5_a_call_through_the_app_cuts_claudes_words_once_and_the_speaker_has_the_switch_the_call_started_with(local, monkeypatch):
    """G5 on a loopback WebRTC call through the app (fake voice model, fake Claude): the holding line, Claude's first
    piece, then the second piece as a whole sentence with g5.second_piece, and as "(spoken," plus the rest with it off.
    The speaker gets the switch as the call started (read once, not per piece)."""
    from app.voicemouth import FILLERS
    _needs_pipecat()
    monkeypatch.setattr(voicertc, "ALLOW_LOOPBACK", True)
    local.app.state.openers = OpenerCache(lambda text, lang: (b"\x00\x10" * 2400, 24000))       # no warm-up noise
    seen, real = [], RtcSpeaker.feed

    def spy(self, delta):
        seen.append(self.second_piece)
        return real(self, delta)
    monkeypatch.setattr(RtcSpeaker, "feed", spy)
    assert local.put("/api/flags/g5.stream_verbatim", json={"on": True}).status_code == 200
    got = {}
    for value in (False, True):
        assert local.put("/api/flags/g5.second_piece", json={"on": value}).status_code == 200
        seen.clear()
        phone = asyncio.run(_app_call(local, seconds=25.0))
        assert seen and set(seen) == {value}, (value, seen)
        got[value] = [e["text"] for e in phone.events if e.get("type") == "say"]
    for value, pieces in got.items():
        assert pieces[0] == FILLERS["en"][0] and pieces[1] == "echo:", (value, pieces)       # the holding line, then Claude's first piece
    assert got[False][2] == "(spoken,"                                                       # the second piece cut again at its first clause
    assert got[True][2].startswith("(spoken, on a call) Remind me to call the dentist tomorrow")   # a whole sentence
    assert len(got[True]) < len(got[False])
