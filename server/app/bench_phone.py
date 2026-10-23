"""The fake phone of the voice latency harness (app/bench_voice.py), and the mock call it can talk to.

Phone: an aiortc peer that does what the iPhone PWA does (web/src/rtclink.js): a microphone track that plays the
corpus clips at real time (20 ms frames at 48 kHz, silence between), the same data channel ("kairo", with the
"ping" it sends), and a receiver that meters Kairo's voice (RMS per frame; 0.01 is "playing", as on the phone).
No STUN or TURN server is asked (iceServers=[]), and only tailnet addresses are offered (voicertc.tailnet_only).

Bench: the plan. Every clip that should be answered (question, statement, command) is played once when the line
is quiet and timed from its true end of speech to the first audible reply frame (total_ms) and from the
server's own timing event for that turn (stages). While Kairo is talking, a backchannel or a noise is played and
we look at whether it stopped (false interrupt); a stranger's voice is played alone and the speaker check's
verdict read from the data channel ("ignored", else answered).

How "interrupted" is decided, from the phone's side only: Kairo was speaking when the clip started (audible
reply frames in the half second before), the PC's VAD heard it (a "speech" event on the data channel, which
is what makes Pipecat cut the answer), and the reply then went silent for 0.5 s or more. All three are needed:
a reply that ends by itself at that moment has no "speech" event, a noise the VAD ignores has none either.

MockPipeline: the real call pipeline (voicertc.build_call: Silero VAD, Smart Turn v3, Pipecat) in this process, with
the models faked by stand-ins that wait fixed, known times (MockDelays): speech to text, the voice model's
first word, the voice. The stand-in "speech to text" is told which clip is playing (MockWorld.current), the
stand-in speaker check admits every third stranger. The harness's tests rest on those known numbers.
"""

from __future__ import annotations

import asyncio
import contextlib
import fractions
import functools
import io
import json
import logging
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Awaitable, Callable

from . import bench_corpus as bc
from .bench_corpus import ANSWERED, PROBES, Clip, Rendered
from .bench_voice import MISSED_AFTER_S, BenchError, stages_from_server, turn_record

log = logging.getLogger("kairo.bench")

RATE = 48000                  # the phone's microphone
FRAME = 960                   # 20 ms
PLAYING_RMS = 0.01            # a reply frame this loud is heard (rtclink.js PLAYING_RMS)
STILL_S = 1.0                 # silence that means the reply is over ...
PROBE_AFTER_ONSET_S = 1.0     # a probe starts this long after Kairo's reply began
PROBE_WATCH_S = 2.0           # and is watched until this long after it ends
CUT_S = 0.5                   # a reply silent this long, after a probe, was cut off ...
CUT_LAG_S = 0.8               # ... when the silence began within this long after the PC's VAD heard the probe
SPEAKING_WINDOW_S = 0.5       # Kairo is "speaking" if a loud frame came in the last half second


class Phone:
    """A fake iPhone on a WebRTC call to Kairo."""

    def __init__(self, still_s: float = STILL_S) -> None:
        self.still_s = still_s                             # silence that means Kairo's reply is over
        self.pc = self.mic = self.channel = None
        self.frames: deque = deque(maxlen=60000)           # (monotonic time, RMS) of every reply frame
        self.events: list[tuple[float, dict]] = []         # (monotonic time, event) from the data channel
        self.last_loud: float | None = None
        self._tasks: list[asyncio.Task] = []
        self._pcm: dict[str, object] = {}

    # ---------------------------------------------------------------- the call

    async def connect(self, signal: Callable[[str, str], Awaitable[dict]], *, timeout: float = 20.0) -> None:
        """signal(sdp, type) -> {"sdp", "type"}: the offer to Kairo, its answer back (HTTP, or the mock)."""
        import numpy as np
        from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))          # none: the default would ask a public STUN server
        self.mic = _mic_class()()
        self.pc.addTrack(self.mic)
        self.channel = self.pc.createDataChannel("kairo")

        @self.channel.on("message")
        def on_message(msg):
            if isinstance(msg, str) and msg[:1] == "{":
                try:
                    self.events.append((time.monotonic(), json.loads(msg)))
                except ValueError:
                    pass

        @self.pc.on("track")
        def on_track(track):
            async def read():
                while True:
                    try:
                        f = await track.recv()
                    except Exception:
                        return
                    t = time.monotonic()
                    a = f.to_ndarray().astype(np.float32) / 32768.0
                    rms = float(np.sqrt(np.mean(a * a))) if a.size else 0.0
                    self.frames.append((t, rms))
                    if rms >= PLAYING_RMS:
                        self.last_loud = t
            self._tasks.append(asyncio.get_running_loop().create_task(read()))

        async def pings():
            while True:
                await asyncio.sleep(0.5)
                if self.channel.readyState == "open":
                    self.channel.send("ping")
        self._tasks.append(asyncio.get_running_loop().create_task(pings()))
        await self.pc.setLocalDescription(await self.pc.createOffer())
        answer = await signal(self.pc.localDescription.sdp, self.pc.localDescription.type)
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not (self.channel.readyState == "open" and self.frames and self.mic.t0):
            await asyncio.sleep(0.05)
        if not (self.channel.readyState == "open" and self.frames):
            raise BenchError("the call did not come up (no data channel or no audio from Kairo within "
                             f"{timeout:.0f} s); is Tailscale up on this PC?")
        await asyncio.sleep(0.5)

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.pc.close(), 5)

    # ---------------------------------------------------------------- playing a clip

    def schedule(self, rc: Rendered) -> tuple[float, float]:
        """Start the clip at the next 20 ms frame. (when its first sample is captured, when its speech ends),
        monotonic seconds; the clip then plays on by itself, followed by silence."""
        import numpy as np
        from .voicelab import resample, to_pcm16
        key = f"{rc.clip.id}:{rc.source}:{len(rc.samples)}"
        if key not in self._pcm:
            x = resample(np.asarray(rc.samples, dtype=np.float32), rc.rate, RATE)
            self._pcm[key] = np.frombuffer(to_pcm16(x), dtype="<i2").copy()
        start = self.mic.play(self._pcm[key])
        return start, start + rc.end_s

    # ---------------------------------------------------------------- what came back

    def loud(self, lo: float, hi: float = float("inf")) -> list[float]:
        """Times of the loud reply frames in [lo, hi], oldest first (newest frames are looked at first)."""
        out = []
        for t, rms in reversed(self.frames):
            if t < lo:
                break
            if t <= hi and rms >= PLAYING_RMS:
                out.append(t)
        return out[::-1]

    def speaking(self, at: float) -> bool:
        return bool(self.loud(at - SPEAKING_WINDOW_S, at))

    def event(self, kind: str, after: float, before: float = float("inf")) -> dict | None:
        for t, e in list(self.events):
            if t >= after and t <= before and e.get("type") == kind:
                return e
        return None

    def event_time(self, kind: str, after: float, before: float = float("inf")) -> float | None:
        for t, e in list(self.events):
            if after <= t <= before and e.get("type") == kind:
                return t
        return None

    async def wait_for(self, after: float, until: float, kinds: tuple[str, ...] = ()) -> tuple[float | None, str | None]:
        """(time, what) of the first of: a loud reply frame ("reply") or one of the events `kinds`, at or after
        `after`; (None, None) at `until`."""
        while True:
            loud = self.loud(after)
            seen = [(self.event_time(k, after), k) for k in kinds]
            first = min([(t, w) for t, w in [(loud[0] if loud else None, "reply"), *seen] if t is not None], default=None)
            if first is not None:
                return first
            if time.monotonic() >= until:
                return None, None
            await asyncio.sleep(0.005)

    async def quiet(self, patience: float = 40.0) -> bool:
        """Wait until Kairo has been silent for still_s and said it is done speaking (or for 4x as long)."""
        deadline = time.monotonic() + patience
        while time.monotonic() < deadline:
            now = time.monotonic()
            last = self.last_loud
            if last is None or (now - last >= self.still_s and (self.event_time("spoken", last) is not None
                                                                 or now - last >= 4 * self.still_s)):
                return True
            await asyncio.sleep(0.05)
        return False

    def was_cut(self, probe_start: float, probe_end: float, now: float | None = None) -> bool:
        """Kairo was speaking when the probe began, the PC's VAD heard it ("speech"), and the reply fell silent for
        CUT_S, starting within a moment of that event (Pipecat stops the voice when the VAD hears the owner)."""
        if not self.speaking(probe_start):
            return False
        heard = self.event_time("speech", probe_start - 0.05, probe_end + PROBE_WATCH_S)
        if heard is None:
            return False
        marks = [*self.loud(probe_start - SPEAKING_WINDOW_S), time.monotonic() if now is None else now]
        return any(heard - 0.1 <= a <= heard + CUT_LAG_S and b - a >= CUT_S for a, b in zip(marks, marks[1:]))


@functools.lru_cache(maxsize=1)
def _mic_class():
    import numpy as np
    from aiortc import MediaStreamTrack
    from av import AudioFrame

    class Mic(MediaStreamTrack):
        """The phone's microphone: silence, and a clip when play() is called, at real-time pace."""
        kind = "audio"

        def __init__(self):
            super().__init__()
            self.t0, self.n, self.clip = None, 0, None

        async def recv(self):
            if self.t0 is None:
                self.t0 = time.monotonic()
            self.n += 1
            await asyncio.sleep(max(0.0, self.t0 + self.n * 0.02 - time.monotonic()))
            at = (self.n - 1) * FRAME
            chunk = np.zeros(FRAME, dtype=np.int16)
            if self.clip is not None:
                start, data = self.clip
                if at >= start:
                    part = data[at - start:at - start + FRAME]
                    chunk[:len(part)] = part
                    if at - start + FRAME >= len(data):
                        self.clip = None
            f = AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
            f.sample_rate, f.pts, f.time_base = RATE, self.n * FRAME, fractions.Fraction(1, RATE)
            return f

        def play(self, data) -> float:
            start = self.n * FRAME                    # the frame after the one being waited for
            self.clip = (start, data)
            return self.t0 + start / RATE
    return Mic


# ---------------------------------------------------------------- the plan

class Bench:
    """Runs the corpus against a connected Phone and returns the schema's turn records."""

    def __init__(self, phone: Phone, clips: list[Rendered], *, speaker_check: bool | None = None, warmup: int = 1,
                 before: Callable[[Rendered, bool], None] | None = None, notes: list[str] | None = None,
                 miss_after_s: float = MISSED_AFTER_S) -> None:
        self.phone, self.warmup, self.before = phone, warmup, before or (lambda rc, long_reply: None)
        self.miss_after_s = miss_after_s            # a real utterance with no reply by then is missed (5 s; tests use less)
        self.speaker_check = speaker_check          # True: the check is on (its verdicts are the labels), else None
        self.notes = notes if notes is not None else []
        self.answered = [c for c in clips if c.clip.kind in ANSWERED]
        self.probes = deque(c for c in clips if c.clip.kind in PROBES)
        self.strangers = [c for c in clips if c.clip.kind == "other_speaker"]
        self.records: list[dict] = []
        self.probe_windows: list[tuple[str, float, float]] = []      # (clip id, start, end of speech) of each probe played

    async def run(self) -> list[dict]:
        if not self.answered:
            raise BenchError("the corpus has nothing to ask (no question, statement or command)")
        for rc in self.answered[:1] * self.warmup:                              # the first turn of a call is slower: not measured
            await self._play(rc, long_reply=False, wait=self.miss_after_s)
        for rc in self.answered:
            await self._answered(rc)
        await self._leftover_probes()
        for rc in self.strangers:
            await self._stranger(rc)
        return self.records

    async def _play(self, rc: Rendered, *, long_reply: bool, wait: float) -> tuple[float, float, float | None, str | None]:
        """Wait for a quiet line, play the clip, wait for the first reply frame (or an "ignored" event)."""
        if not await self.phone.quiet():
            self._note(f"{rc.clip.id}: Kairo was still talking 40 s after the last clip; played it anyway")
        self.before(rc, long_reply)
        start, end = self.phone.schedule(rc)
        when, what = await self.phone.wait_for(end, end + wait, ("ignored",))
        return start, end, when, what

    async def _answered(self, rc: Rendered) -> None:
        carrier = bool(self.probes)
        start, end, when, what = await self._play(rc, long_reply=carrier, wait=self.miss_after_s)
        if what == "reply":
            total = (when - end) * 1000
            timing = await self._timing(start, when)
            self.records.append(turn_record(rc.clip, stages=stages_from_server((timing or {}).get("server"), total), total_ms=total,
                                            speaker_accept=True if self.speaker_check else None))
            if carrier:
                await self._probe(when)
            return
        refused = what == "ignored"
        if refused:
            self._note(f"{rc.clip.id}: the speaker check refused it ({self.phone.event('ignored', start).get('reason')})"
                       + ("" if rc.source == "own" else "; a synthetic voice is not the owner: not counted as missed"))
        self.records.append(turn_record(rc.clip, missed=not refused or rc.source == "own", speaker_accept=False if refused else None))
        await asyncio.sleep(1.0)                                              # a late answer may still come: let it end first

    def _note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    async def _timing(self, start: float, onset: float) -> dict | None:
        deadline = max(onset, time.monotonic()) + 1.0
        while time.monotonic() < deadline:
            e = self.phone.event("timing", start)
            if e is not None:
                return e
            await asyncio.sleep(0.01)
        self._note("no timing event from the server for a turn: its stages are empty")
        return None

    async def _probe(self, onset: float) -> bool:
        """Kairo is talking (its reply began at `onset`): play the next backchannel or noise over it."""
        at = onset + PROBE_AFTER_ONSET_S
        await asyncio.sleep(max(0.0, at - 0.02 - time.monotonic()))
        if not self.probes or not self.phone.speaking(time.monotonic()):
            return False                                                       # its answer was too short to talk over
        rc = self.probes.popleft()
        self.before(rc, False)
        start, end = self.phone.schedule(rc)
        self.probe_windows.append((rc.clip.id, start, end))
        await asyncio.sleep(max(0.0, end + PROBE_WATCH_S - time.monotonic()))
        self.records.append(turn_record(rc.clip, interrupted=self.phone.was_cut(start, end)))
        return True

    async def _leftover_probes(self) -> None:
        """Probes that found Kairo already quiet: ask the longest question again as a carrier (not recorded)."""
        carrier = max(self.answered, key=lambda c: len(c.clip.text))
        tries = 0
        while self.probes and tries < 2 * len(self.probes) + 2:
            tries += 1
            start, end, when, what = await self._play(carrier, long_reply=True, wait=self.miss_after_s)
            if what == "reply":
                await self._probe(when)
        for rc in self.probes:                                                  # never found a reply long enough to talk over
            self._note(f"{rc.clip.id}: Kairo's replies were too short to talk over; no false-interrupt test")
            self.records.append(turn_record(rc.clip, interrupted=None))
        self.probes.clear()

    async def _stranger(self, rc: Rendered) -> None:
        """A stranger's voice, alone on the line: the speaker check's verdict is the data channel's "ignored" (not you,
        too quiet) or, when it let the turn through, an answer."""
        start, end, when, what = await self._play(rc, long_reply=False, wait=self.miss_after_s)
        ignored, transcript = self.phone.event("ignored", start), self.phone.event("transcript", start)
        accept = None
        if self.speaker_check:
            if ignored is not None:
                accept = False
            elif what == "reply" or transcript is not None:
                accept = True
        elif what is not None or transcript is not None:
            self._note("the speaker check is off or has no voiceprint: other_speaker clips can't be labelled")
        self.records.append(turn_record(rc.clip, speaker_accept=accept))


# ---------------------------------------------------------------- the mock call

@dataclass
class MockDelays:
    """What the stand-in models wait, in seconds: the numbers a run has to find again."""
    stt_s: float = 0.12
    llm_s: float = 0.15
    tts_s: float = 0.08


@dataclass
class MockWorld:
    """What the stand-ins know that the real models would hear: which clip is playing, and what happened."""
    delays: MockDelays = field(default_factory=MockDelays)
    current: Clip | None = None
    long_reply: bool = False
    stranger_every: int = 3                                  # the stand-in speaker check admits every 3rd stranger
    deaf: set = field(default_factory=set)                   # clip ids the stand-in speech to text hears nothing in
    interrupts: list = field(default_factory=list)           # (monotonic, turn number, cut mid-speech?) each time the PC stops its answer
    verdicts: list = field(default_factory=list)             # (clip id, admitted) for each stranger, in the order they came
    answers: list = field(default_factory=list)              # (turn number, text)
    audio_until: float = 0.0                                 # when the voice's queued audio would be played out (monotonic)

    def before(self, rc: Rendered, long_reply: bool) -> None:
        self.current, self.long_reply = rc.clip, long_reply


def _tone_wav(seconds: float, rate: int = 24000) -> bytes:
    import numpy as np
    t = np.arange(int(seconds * rate)) / rate
    pcm = (8000 * np.sin(2 * np.pi * 440 * t)).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class MockVoice:
    """Whisper and Kairo's voice as stand-ins (voicertc's `voice`): fixed waits, fixed content."""

    def __init__(self, world: MockWorld) -> None:
        self.world = world

    def transcribe_pcm(self, pcm: bytes, language=None):
        time.sleep(self.world.delays.stt_s)
        clip = self.world.current
        text = "" if clip is None or clip.kind == "noise" or clip.id in self.world.deaf else clip.text
        return SimpleNamespace(text=text, language=clip.lang if clip else "en", seconds=len(pcm) / 32000)

    def speak(self, text: str, language=None):
        time.sleep(self.world.delays.tts_s)
        seconds = 0.25 + 0.07 * len(text.split())
        self.world.audio_until = max(self.world.audio_until, time.monotonic()) + seconds
        return _tone_wav(seconds), language or "en"


SHORT_REPLY = ("Right, I have that for you.",)
LONG_REPLY = tuple(f"Here is part {i} of a longer answer." for i in range(1, 5))        # about 3.5 s of voice


class MockPipeline:
    """The call pipeline (voicertc.build_call) with MockVoice, a stand-in voice model and a stand-in speaker check."""

    def __init__(self, world: MockWorld) -> None:
        self.world, self.conn, self.call, self.run = world, None, None, None

    async def signal(self, sdp: str, kind: str) -> dict:
        from . import voicertc
        w = self.world
        self.conn = await voicertc.connect(sdp, kind, allow_loopback=True)

        async def answer(text, language, number, speaker):
            w.answers.append((number, text))
            await asyncio.sleep(w.delays.llm_s)
            for line in (LONG_REPLY if w.long_reply else SHORT_REPLY):
                speaker.feed(line + " ")
            speaker.finish()
            return {"route": "talk"}

        async def gate() -> bool:
            clip = w.current
            if clip is None or clip.kind != "other_speaker":
                return True
            known = dict(w.verdicts)                         # one verdict per clip, even if the PC heard it as two turns
            admitted = known[clip.id] if clip.id in known else len(known) % w.stranger_every == 0
            if clip.id not in known:
                w.verdicts.append((clip.id, admitted))
            if not admitted:
                self.conn.send_app_message({"type": "ignored", "reason": "not_you", "score": 0.1})
            return admitted

        class NoCache(voicertc.OpenerCache):
            """Short lines would be played from the cache after the first time: the voice's wait must show every time."""

            def get(self, text, lang):
                return None

            def put(self, *args, **kwargs):
                return None

        def interrupt(number: int) -> None:
            """The PC calls this whenever the owner starts talking after an answer began (also long after it
            ended); it is a real cut only while that answer's voice was still being played."""
            now = time.monotonic()
            w.interrupts.append((now, number, now < w.audio_until - 0.3))
            w.audio_until = 0.0

        self.call = voicertc.build_call(self.conn, voice=MockVoice(w), answer=answer, openers=NoCache(lambda t, lang: (b"", 24000)),
                                        interrupt=interrupt, gate=gate)
        self.run = asyncio.get_running_loop().create_task(self.call.run())
        ans = self.conn.get_answer()
        return {"sdp": ans["sdp"], "type": ans["type"]}

    async def stop(self) -> None:
        if self.call is not None:
            with contextlib.suppress(Exception):
                await self.call.stop()
        if self.run is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.gather(self.run, return_exceptions=True), 10)


def mock_clips(clips: list[Clip]) -> list[Rendered]:
    """The corpus as speech-like audio made on the spot (bench_corpus.synth_speech / synth_noise)."""
    out = []
    for c in clips:
        if bc.recipe_of(c):
            x, rate = bc.synth_noise(c), 24000
        else:
            x, rate = bc.synth_speech(c.text), 16000
        out.append(Rendered(c, x, rate, bc.speech_end_s(x, rate), "synthetic", "synthetic"))
    return out


# ---------------------------------------------------------------- the network rules

@contextlib.contextmanager
def ice_rules(loopback: bool):
    """WebRTC for this run: tailnet addresses only (voicertc.tailnet_only, as on the phone), plus 127.0.0.1 when the
    phone and the pipeline are both in this process (the mock). The rule that was in force comes back afterwards."""
    import aioice.ice
    from . import voicertc
    before = (aioice.ice.get_host_addresses, voicertc._allow_loopback)
    try:
        voicertc.tailnet_only(allow_loopback=loopback)
        yield
    finally:
        aioice.ice.get_host_addresses, voicertc._allow_loopback = before
