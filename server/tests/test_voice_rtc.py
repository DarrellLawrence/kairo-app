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
import math
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import voicertc
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
    assert _drain(s) == ["Sure,", "I'll remind you tomorrow morning.", "And I added it to your list.",
                         "Btw, did you pay the phone bill?", "It's due today."]


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


def _phone_track(times: list[float]):
    """The phone's microphone: half a second of room, then speech at each of `times` (seconds after the
    start), silence otherwise; real-time pace, 20 ms frames at 48 kHz."""
    import numpy as np
    from aiortc import MediaStreamTrack
    from av import AudioFrame
    speech = _speech_48k()

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
                voice=None, openers=None, greeting=None, first_message=None, warning=None):
    """A fake phone calls Kairo over WebRTC on this machine. Returns what the phone saw."""
    import numpy as np
    from aiortc import RTCPeerConnection, RTCSessionDescription
    voicertc.tailnet_only(allow_loopback=True)      # the fake phone is in this process: it offers 127.0.0.1 too
    voice = voice or FakeVoice()
    phone = SimpleNamespace(events=[], audio_after=[], mic=None, loud=[])
    pc = RTCPeerConnection()
    mic = _phone_track(list(times))
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
                               warning=warning)
    run = asyncio.get_running_loop().create_task(call.run())
    ans = conn.get_answer()
    await pc.setRemoteDescription(RTCSessionDescription(sdp=ans["sdp"], type=ans["type"]))
    pinger = asyncio.get_running_loop().create_task(pings())
    try:
        deadline = time.monotonic() + timeout
        while not wait_for(phone) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
    finally:
        pinger.cancel()
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


# ---------------------------------------------------------------- through the app (/api/rtc/offer)

from tests.test_voice_local import local  # noqa: E402,F401  (the app with the fake voice model and Claude)


async def _app_call(client, *, path="call", seconds=12.0):
    """The fake phone calls the app: offer over HTTP, audio over WebRTC on this machine."""
    import numpy as np
    from aiortc import RTCPeerConnection, RTCSessionDescription

    voicertc.tailnet_only(allow_loopback=True)
    phone = SimpleNamespace(events=[], audio_after=[])
    pc = RTCPeerConnection()
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
