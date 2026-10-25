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
from app.voicertc import OpenerCache, RtcSpeaker, Timeline, offer_on_tailnet, on_tailnet

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
                gate=None):
    """A fake phone calls Kairo over WebRTC on this machine. Returns what the phone saw.
    later: (seconds after the mic started, message[, event]) sent on the data channel then, once the PC sent
    that event (the phone taps "done" only after the PC said it hears speech); phone.sent: when."""
    import numpy as np
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    voicertc.tailnet_only(allow_loopback=True)      # the fake phone is in this process: it offers 127.0.0.1 too
    voice = voice or FakeVoice()
    phone = SimpleNamespace(events=[], audio_after=[], mic=None, loud=[], sent=[])
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))   # like the phone: no STUN/TURN (aiortc's default asks Google)
    mic = _phone_track(list(times), speech)
    phone.mic = mic
    pc.addTrack(mic)
    channel = pc.createDataChannel("kairo")

    @channel.on("message")
    def on_message(msg):
        try:
            phone.events.append(json.loads(msg))
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
    call = voicertc.build_call(conn, voice=voice, answer=answer, interrupt=interrupt, openers=openers, greeting=greeting,
                               warning=warning, gate=gate)
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
                              timeout=30))
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
    assert "ended" not in transcript


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
