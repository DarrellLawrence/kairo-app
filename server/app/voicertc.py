"""Voice V3 (owner's request 2026-10-16): calls and the voice button over WebRTC, on Pipecat.

The phone sends its microphone as WebRTC audio (Opus, the iPhone's own echo cancellation) straight to
this PC over the tailnet and plays Kairo's voice from the same connection; a data channel carries the
events (transcripts, what Kairo says, per-turn times). One call is one Pipecat pipeline:

  WebRTC in -> Silero VAD (0.2 s pause) -> Whisper (at that pause) -> user turn (Smart Turn v3.2 decides
  if the owner is done) -> the voice model's conversation (voicemouth.Mouth: Today card, ask_kairo,
  holding line + bring-up, reports) -> Kokoro / Piper (first clause first, openers from a cache) -> out

Talking over Kairo is an interruption: Pipecat stops the audio at once and the voice model's turn is
cancelled (what was said is kept, a pending Claude answer comes later, as before).

"I'm done" (owner's request 2026-10-20: "when I'm around people I can just tap it and then it knows I'm done
talking"): a loud room can keep the VAD hearing speech, so no pause ever ends the turn. A tap on the phone
sends {"type": "done"}; if anything was heard since the last turn, the turn ends as soon as Whisper has
written it down (the VAD's speech is cut at the tap; Smart Turn's verdict and its pause are not waited for)
and goes on to the same acceptance point as any turn (speaker check + quiet gate, then the answer).
Recorded as ended by "tap", timed from the tap.

Everything runs in Kairo's own process: the models Kairo already loaded (Whisper, Kokoro, Piper, the
llama.cpp voice model) are used as they are. Pipecat and aiortc come with the voice packages
(requirements-voice.txt); without them `available()` is False and the old line (/ws/voice) answers.

Per turn the PC times its own steps from the end of speech the VAD saw (vad -> stt -> turn -> first
word -> first audio out) and sends them on the data channel; the phone adds when the audio started
playing and the network (WebRTC round trip, direct or relayed): app/turns.py records them all.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import rtcjitter
from .voicelink import first_clause

log = logging.getLogger("kairo.voicertc")

VAD_STOP_S = 0.2              # the first short pause: Whisper starts, Smart Turn decides
# Smart Turn says "not done" but the owner stays quiet: the turn ends anyway after this. 0.5 s keeps such a
# turn inside the 1.0 s target (0.2 + 0.5 + the answer's first audio); the price is that a long pause
# mid-thought gets an answer started, which stops the moment the owner talks on (and the voice model
# started early on those words, so little is lost).
TURN_MAX_SILENCE_S = 0.5
TRAILING_SILENCE_S = 0.2      # Whisper gets this much silence after the words (it ends the last word)
# After a tap the VAD hears nothing until that turn is taken (a loud room would start a new stretch of speech,
# which holds the turn open, before Whisper is through); never longer than this.
TAP_HOLD_S = 3.0
OUT_RATE = 24000              # Kokoro's rate; Piper (22.05 kHz) is resampled to it
OPENERS = {
    "en": ("Sure,", "Sure.", "Okay,", "Okay.", "Yeah,", "Yeah.", "Got it.", "Hey!", "Hm,", "Right,", "Nice.", "Done."),
    "de": ("Klar,", "Klar.", "Okay,", "Okay.", "Ja,", "Ja.", "Alles klar.", "Hey!", "Hm,", "Gut.", "Erledigt."),
}


def available() -> bool:
    """Pipecat + aiortc are installed (they come with the voice packages)."""
    try:
        import aiortc  # noqa: F401
        import pipecat  # noqa: F401
    except Exception:
        return False
    return True


# ---------------------------------------------------------------- the tailnet only (no public ports)

TAILNET = ("100.64.0.0/10", "fd7a:115c:a1e0::/48")      # Tailscale's address ranges
ALLOW_LOOPBACK = False                                   # tests only (the fake phone on 127.0.0.1): never set in Kairo
_allow_loopback = False


def on_tailnet(ip: str) -> bool:
    import ipaddress
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if _allow_loopback and addr.is_loopback:
        return True
    return any(addr in ipaddress.ip_network(n) for n in TAILNET)


def tailnet_only(allow_loopback: bool = False) -> None:
    """WebRTC audio uses the tailnet and nothing else: this PC offers only its Tailscale addresses (aiortc
    would offer every network card, the home LAN included) and no STUN/TURN server is ever asked."""
    global _allow_loopback
    import aioice.ice
    _allow_loopback = allow_loopback
    original = getattr(aioice.ice.get_host_addresses, "_kairo_original", aioice.ice.get_host_addresses)

    def tailnet_addresses(use_ipv4: bool, use_ipv6: bool) -> list[str]:
        found = [a for a in original(use_ipv4, use_ipv6) if on_tailnet(a)]
        if allow_loopback and use_ipv4:
            found.append("127.0.0.1")
        return found
    tailnet_addresses._kairo_original = original
    aioice.ice.get_host_addresses = tailnet_addresses


def tailnet_addresses() -> list[str]:
    """This PC's Tailscale addresses (none: Tailscale is down, a call can't go over WebRTC now)."""
    import aioice.ice
    get = getattr(aioice.ice.get_host_addresses, "_kairo_original", aioice.ice.get_host_addresses)
    return [a for a in get(True, True) if on_tailnet(a) and not ipaddress_is_loopback(a)]


def ipaddress_is_loopback(ip: str) -> bool:
    import ipaddress
    try:
        return ipaddress.ip_address(ip.split("%")[0]).is_loopback
    except ValueError:
        return False


def offer_on_tailnet(sdp: str) -> str:
    """The phone's offer with only its tailnet candidates (a candidate for its Wi-Fi or mobile address,
    or an mDNS name, is dropped: this PC never sends to them). Its tailnet address may also show up only
    when its first check arrives (peer reflexive): that works too."""
    out = []
    for line in sdp.splitlines():
        if line.startswith("a=candidate:"):
            bits = line.split()
            if len(bits) < 6 or not on_tailnet(bits[4]) or bits[2].lower() != "udp":
                continue
        out.append(line)
    return "\r\n".join(out) + "\r\n"


async def connect(sdp: str, kind: str = "offer", *, allow_loopback: bool = False, jitter_patch: bool = False):
    """The phone's offer in, this PC's answer out: a SmallWebRTCConnection on the tailnet only (no STUN, no
    TURN: the phone and this PC reach each other over Tailscale, direct or through its relay).
    jitter_patch (flag g4.jitter_patch, read by the caller at the start of the call): the audio jitter buffer
    gets 8 packets that waits for 1 instead of 16 and 4 (rtcjitter.py). Here, because the library builds its
    receivers while it takes the offer: build_call comes after, and the first packet after the answer."""
    from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
    tailnet_only(allow_loopback=allow_loopback or ALLOW_LOOPBACK)
    conn = SmallWebRTCConnection(ice_servers=[])
    await conn.initialize(sdp=offer_on_tailnet(sdp), type=kind)
    if jitter_patch:
        rtcjitter.install(getattr(conn, "pc", None))      # never raises: the patch is an extra, the call is not
    return conn


# ---------------------------------------------------------------- one turn's times

@dataclass
class Timeline:
    """One turn, in monotonic seconds; ms() turns it into steps from the end of speech."""
    vad_stop: float | None = None        # the VAD noticed the pause (VAD_STOP_S after the last word)
    stt_start: float | None = None
    stt_done: float | None = None
    turn_end: float | None = None        # Smart Turn (or the long pause, or a tap) ended the turn
    tapped: float | None = None          # the owner tapped "done" then: the turn ended by the tap, timed from it
    first_token: float | None = None     # the voice model's first word or tool call
    tts_start: float | None = None
    first_audio: float | None = None     # the first audio of the answer went to the phone
    cached: bool = False                 # the first audio came from the opener cache
    text: str = ""
    language: str | None = None
    asked: bool = False                  # it went to Claude (ask_kairo)
    route: str | None = None
    marks: dict = field(default_factory=dict)

    def ms(self) -> dict:
        """Server steps (ms). The end of speech is VAD_STOP_S before the VAD noticed it, or the tap."""
        if self.vad_stop is None:
            return {}
        end = self.tapped if self.tapped is not None else self.vad_stop - VAD_STOP_S

        def at(t):
            return None if t is None else max(0, round((t - end) * 1000))
        out = {"vad_ms": 0 if self.tapped is not None else int(VAD_STOP_S * 1000)}
        if self.stt_start is not None and self.stt_done is not None:
            out["stt_ms"] = round((self.stt_done - self.stt_start) * 1000)
        for key, t in (("turn_at_ms", self.turn_end), ("first_token_at_ms", self.first_token),
                       ("tts_at_ms", self.tts_start), ("first_audio_at_ms", self.first_audio)):
            v = at(t)
            if v is not None:
                out[key] = v
        if self.first_audio is not None and self.tts_start is not None:
            out["tts_ms"] = round((self.first_audio - self.tts_start) * 1000)
        if self.first_token is not None and self.turn_end is not None:
            out["llm_ms"] = round((self.first_token - self.turn_end) * 1000)
        out["cached_opener"] = self.cached
        return out


# ---------------------------------------------------------------- text into speech, first clause first

class OpenerCache:
    """Short lines Kairo says often, made once, played at once (the holding lines, 'Sure,' ...)."""

    def __init__(self, speak: Callable[[str, str], tuple[bytes, int]], *, limit: int = 200):
        self.speak, self.limit = speak, limit
        self.audio: dict[tuple[str, str], tuple[bytes, int]] = {}

    @staticmethod
    def key(text: str) -> str:
        return re.sub(r"\s+", " ", text.strip().lower())

    def get(self, text: str, lang: str) -> tuple[bytes, int] | None:
        return self.audio.get((lang, self.key(text)))

    def put(self, text: str, lang: str, pcm: bytes, rate: int, *, any_length: bool = False) -> None:
        """Short pieces of answers are kept as they are said; the lines warm() makes at start, whatever length."""
        if (any_length or len(text.split()) <= 6) and len(self.audio) < self.limit:
            self.audio[(lang, self.key(text))] = (pcm, rate)

    def warm(self, lines: dict[str, tuple[str, ...]]) -> int:
        n = 0
        for lang, texts in lines.items():
            for text in texts:
                if self.get(text, lang) is None:
                    try:
                        pcm, rate = self.speak(text, lang)
                    except Exception as exc:   # a voice that isn't installed: no cache for it
                        log.info("opener cache: %s (%s): %s", text, lang, exc)
                        break
                    self.put(text, lang, pcm, rate, any_length=True)
                    n += 1
        return n


def resample(pcm: bytes, rate: int, to: int) -> bytes:
    if rate == to:
        return pcm
    import numpy as np
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    n = int(len(x) * to / rate)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype("<i2").tobytes()


# ---------------------------------------------------------------- what Kairo says, piece by piece

class RtcSpeaker:
    """The speaker an answer talks through on a WebRTC call (the same feed/say/finish/stop/done as
    voicelink.Speaker): the first piece goes to the voice at its first clause, the rest by sentence,
    Kairo's own lines (holding line, bring-up) as they are, all in order. stop(): talked over, the rest
    is dropped."""

    second_piece = True                      # g5.second_piece (set per call): only the first piece is cut at five words

    def __init__(self, turn: int = 0, whole_numbers: bool = False) -> None:
        from .voicelink import Sentences
        self.turn = turn
        self.whole_numbers = whole_numbers   # g9.speaknorm: no piece ends on a number that wants its unit
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.first = ""                      # the first piece, until it's cut
        self.cut = False
        self.rest = Sentences(whole_numbers)
        self.closed = False
        self.stopped = False
        self.sent = 0
        self.server: dict = {}                # the PC's steps so far (speech to text ...), for the record
        self._done = asyncio.Event()

    def _put(self, piece: str) -> None:
        from .voicelink import speakable
        piece = speakable(piece)
        if piece and not self.stopped:
            self.sent += 1
            self.queue.put_nowait(piece)

    def feed(self, delta: str) -> None:
        if self.closed or self.stopped or not delta:
            return
        if not self.cut:
            self.first += delta
            at = first_clause(self.first, whole_numbers=self.whole_numbers)
            if at is None:
                return
            piece, more = self.first[:at], self.first[at:]
            self.first, self.cut = "", True
            if self.second_piece:
                self.rest.count = 1          # the first piece is out: the rest goes by sentence, not cut at five words again
            self._put(piece)
            delta = more
        for piece in self.rest.feed(delta):
            self._put(piece)

    def flush(self) -> None:
        """The end of what is being said (Claude's text is finished): the words still being collected go to the voice now,
        not when the speaker closes. The speaker stays open; words after this start a fresh first piece."""
        if not self.closed and not self.stopped:
            self._flush()

    def _flush(self) -> None:
        if not self.cut and self.first.strip():
            self._put(self.first)
            self.first = ""
        rest = self.rest.flush()
        if rest:
            self._put(rest)
        self.cut, self.first = False, ""        # after one of Kairo's own lines, the next words start fresh
        from .voicelink import Sentences
        self.rest = Sentences(self.whole_numbers)

    def say(self, text: str) -> None:
        if self.closed or self.stopped:
            return
        self._flush()
        self._put(text)

    def finish(self, final_text: str | None = None) -> None:
        if self.closed:
            return
        self.closed = True
        if not self.stopped:
            if final_text and self.sent == 0 and not self.first.strip() and not self.rest.buf.strip():
                self._put(final_text)
            else:
                self._flush()
        self.queue.put_nowait(None)

    def stop(self) -> None:
        if not self.stopped:
            self.stopped = True
            self.queue.put_nowait(None)

    def ended(self) -> None:
        self._done.set()

    async def done(self) -> None:
        await self._done.wait()


# ---------------------------------------------------------------- the pipeline (needs pipecat)

class Call:
    """One call over WebRTC: the pipeline and its per-turn times. Built by build_call()."""

    def __init__(self, conn, task, runner, state):
        self.conn, self.task, self.runner, self.state = conn, task, runner, state

    async def run(self) -> None:
        await self.runner.run(self.task)

    async def stop(self) -> None:
        await self.task.cancel()


class _State:
    """What the pieces of one call share."""

    def __init__(self, *, answer, interrupt, events, language_mode: str, speculate=None, discard=None, gate=None):
        self.answer, self.interrupt, self.events, self.language_mode = answer, interrupt, events, language_mode
        self.speculate, self.discard, self.gate = speculate, discard, gate
        self.heard = ""                        # this turn's words so far (each pause adds what came before it)
        self.turn = Timeline()
        self.language: str | None = None
        self.number = 0
        self.speaker: RtcSpeaker | None = None
        self.speaking: Timeline | None = None   # the turn whose answer is being spoken
        self.vad_on = False                     # the VAD hears speech right now
        self.pending = 0                        # pieces of speech Whisper is still writing down
        self.tap: str | None = None             # "waiting": tapped, the turn ends once Whisper is through; "ended"
        self.tap_at = 0.0

    def tap_now(self) -> bool:
        """The owner tapped "done". False when nothing was heard since the last turn (nothing to end)."""
        if self.tapping() is not None or not (self.vad_on or self.pending or self.heard):
            return False
        self.tap, self.tap_at = "waiting", time.monotonic()
        return True

    def tapping(self) -> str | None:
        """The tap in progress (the VAD hears nothing meanwhile), at most TAP_HOLD_S."""
        if self.tap is not None and time.monotonic() - self.tap_at > TAP_HOLD_S:
            log.warning("rtc: a tapped turn wasn't taken within %.0f s: the call listens as before", TAP_HOLD_S)
            self.tap, self.pending = None, 0
        return self.tap

    def send(self, msg: dict) -> None:
        try:
            self.events(msg)
        except Exception as exc:   # the data channel is gone: the call goes on without captions
            log.info("rtc: event not sent: %s", exc)

    def ahead(self, how: str, *args) -> None:
        """Speculation (start the voice model at a pause; drop it when the owner talks on): never a crash."""
        fn = self.speculate if how == "start" else self.discard
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:
            log.exception("rtc: speculation")


def build_call(conn, *, voice, answer, interrupt: Callable[[int], Any] | None = None,
               events: Callable[[dict], Any] | None = None, language_mode: str = "auto",
               openers: OpenerCache | None = None, smart_turn_model: str | None = None,
               speculate: Callable[[str, str | None], Any] | None = None, discard: Callable[[], Any] | None = None,
               greeting: str | None = None, warning: str | None = None, gate: Callable[[], Any] | None = None,
               norm: Callable[[str, str | None], tuple[str, str | None]] | None = None):
    """The pipeline for one call on an initialized SmallWebRTCConnection.

    voice: transcribe_pcm(pcm16k, language) -> Transcript; speak(text, language) -> (wav, lang).
    answer: async (text, language, turn number, speaker) -> dict | None: answers the owner's turn through
      speaker (feed/say/finish), as /ws/voice does (the voice model, or Claude). Returns what to record.
    interrupt: (turn number) -> None: the owner talked over that turn's answer.
    events: (dict) -> None: to the phone on the data channel (default: the connection's).
    speculate: (words so far, language) -> None: the owner paused; start the answer early (not said).
    discard: () -> None: the owner talked on; what was started early is thrown away.
    greeting: what Kairo says when the phone asks for it ({"type": "greet"}: a call the owner placed).
    warning: said once right after the first line the phone asks for (greet or the call's opening): the
      voice is slow right now (a part of it is off the GPU).
    gate: async () -> bool, asked when a turn is finished: False = not the owner (speaker.py), the turn is
      ignored (no answer, nothing started early kept) and the call keeps listening.
    norm: (text, language) -> (what the voice says, language) (speaknorm.for_call, g9.speaknorm); None: as written.
    """
    from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams, VADState
    from pipecat.frames.frames import (
        AggregatedTextFrame, BotStoppedSpeakingFrame, ControlFrame, Frame, InterruptionFrame, LLMContextFrame,
        LLMFullResponseEndFrame, LLMFullResponseStartFrame, TranscriptionFrame, TTSAudioRawFrame, TTSSpeakFrame,
        UserStoppedSpeakingFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame,
    )
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
    from pipecat.processors.audio.vad_processor import VADProcessor
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
    from pipecat.services.llm_service import LLMService
    from pipecat.services.settings import LLMSettings, STTSettings, TTSSettings
    from pipecat.services.stt_service import SegmentedSTTService
    from pipecat.services.tts_service import TTSService
    from pipecat.transports.base_transport import TransportParams
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
    from pipecat.turns.types import ProcessFrameResult
    from pipecat.turns.user_stop import BaseUserTurnStopStrategy, TurnAnalyzerUserTurnStopStrategy
    from pipecat.turns.user_turn_strategies import UserTurnStrategies
    from pipecat.utils.text.base_text_aggregator import AggregationType

    from .voicelink import heard, wav_pcm

    state = _State(answer=answer, interrupt=interrupt or (lambda n: None), events=events or conn.send_app_message,
                   language_mode=language_mode, speculate=speculate, discard=discard, gate=gate)
    cache = openers or OpenerCache(lambda text, lang: wav_pcm(voice.speak(text, lang)[0]))

    @dataclass
    class Tapped(ControlFrame):
        """The owner tapped "done" (in order with the transcripts, kept through interruptions)."""
        interruptible: bool = field(default=False, init=False)

    @dataclass
    class Transcribed(ControlFrame):
        """Whisper is through with one piece of speech (after its words, if it found any)."""
        interruptible: bool = field(default=False, init=False)

    class OwnerVAD(SileroVADAnalyzer):
        """Silero, deaf while a tap is in progress: the speech it was hearing ends at once, and a loud room
        can't start a new stretch (which would hold the tapped turn open) before the turn is taken."""

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._deaf = False

        async def analyze_audio(self, buffer: bytes) -> VADState:
            if state.tapping() is not None:
                if not self._deaf:
                    self._deaf = True
                    self.set_params(self.params)           # quiet now; it starts afresh afterwards
                return VADState.QUIET
            self._deaf = False
            return await super().analyze_audio(buffer)

    class TapStop(BaseUserTurnStopStrategy):
        """A tap ends the turn as soon as Whisper has written down all that was said, without waiting for a
        pause or Smart Turn's verdict (Smart Turn doesn't get the tapped turn's words, so it can't end it
        a second time)."""

        def __init__(self):
            super().__init__()
            self._text = ""

        async def handle_user_turn_started(self):
            self._text = ""

        async def handle_user_turn_stopped(self):
            self._text = ""

        async def process_frame(self, frame: Frame) -> ProcessFrameResult:
            if isinstance(frame, TranscriptionFrame):
                self._text = f"{self._text} {frame.text}".strip()
            if state.tapping() != "waiting":
                return ProcessFrameResult.CONTINUE
            if isinstance(frame, (Tapped, Transcribed)) and not state.vad_on and not state.pending:
                if self._text:
                    state.tap, state.turn.tapped = "ended", state.tap_at
                    await self.trigger_user_turn_stopped()
                else:
                    state.tap = None                       # nothing was said: nothing to end
            if isinstance(frame, (TranscriptionFrame, Transcribed)):
                return ProcessFrameResult.STOP
            return ProcessFrameResult.CONTINUE

    class Watch(FrameProcessor):
        """The end of speech the VAD saw starts a turn's clock; the phone hears when speech starts (a turn
        is going on: a tap then ends it)."""

        async def process_frame(self, frame: Frame, direction: FrameDirection):
            await super().process_frame(frame, direction)
            if isinstance(frame, VADUserStoppedSpeakingFrame):
                state.vad_on = False
                state.pending += 1               # Whisper writes this piece down now
                state.turn.vad_stop = time.monotonic()
            elif isinstance(frame, VADUserStartedSpeakingFrame):
                state.vad_on = True
                state.turn = Timeline()          # talked on: the clock starts again at the next pause
                state.ahead("drop")
                state.send({"type": "speech"})
            await self.push_frame(frame, direction)

    class WhisperSTT(SegmentedSTTService):
        def __init__(self):
            super().__init__(trailing_silence_secs=TRAILING_SILENCE_S, settings=STTSettings(model=None, language=None))

        @property
        def wants_wav_segments(self) -> bool:
            return False

        async def run_stt(self, audio: bytes):
            text = await self._words(audio)
            state.pending = max(0, state.pending - 1)
            if text:
                yield TranscriptionFrame(text, "owner", time.strftime("%Y-%m-%dT%H:%M:%S"), finalized=True)
            elif not state.heard:                # only noise since the last turn: no turn is going on
                state.send({"type": "quiet"})
            yield Transcribed()

        async def _words(self, audio: bytes) -> str:
            t = state.turn
            t.stt_start = time.monotonic()
            lang = state.language_mode if state.language_mode in ("en", "de") else None
            try:
                tr = await asyncio.to_thread(voice.transcribe_pcm, resample(audio, self.sample_rate, 16000), lang)
            except Exception as exc:   # nothing that sounds like words (or Whisper failed): no turn
                log.info("rtc: speech to text: %s: %s", type(exc).__name__, exc)
                t.stt_done = time.monotonic()
                return ""
            t.stt_done = time.monotonic()
            text = heard(tr.text or "")
            if not text:
                return ""
            t.text = f"{t.text} {text}".strip()
            if tr.language in ("en", "de"):
                t.language = state.language = tr.language
            state.heard = f"{state.heard} {text}".strip()
            state.ahead("start", state.heard, t.language or state.language)   # before Smart Turn has decided
            state.send({"type": "heard", "text": text, "language": tr.language,
                        "stt_ms": int((t.stt_done - t.stt_start) * 1000)})
            return text

    class EndOfTurn(FrameProcessor):
        async def process_frame(self, frame: Frame, direction: FrameDirection):
            await super().process_frame(frame, direction)
            if isinstance(frame, UserStoppedSpeakingFrame):
                state.turn.turn_end = time.monotonic()
            elif isinstance(frame, (Tapped, Transcribed)):
                return                           # the turn strategies' markers end here
            await self.push_frame(frame, direction)

    class Answer(LLMService):
        """Pipecat's LLM slot: the owner's turn goes to answer() (the voice model or Claude); what it says
        comes back through an RtcSpeaker as pieces for the voice, in order."""

        def __init__(self):
            super().__init__(settings=LLMSettings(**{k: None for k in (
                "model", "system_instruction", "temperature", "max_tokens", "top_p", "top_k", "frequency_penalty",
                "presence_penalty", "seed", "filter_incomplete_user_turns", "user_turn_completion_config")}))

        async def process_frame(self, frame: Frame, direction: FrameDirection):
            await super().process_frame(frame, direction)
            if isinstance(frame, LLMContextFrame):
                text = _last_user_text(frame.context)
                state.heard = ""
                if text and state.gate is not None and not await state.gate():   # not the owner (speaker.py)
                    text = ""
                    state.ahead("drop")
                state.tap = None                 # the turn is taken (or ignored): the VAD listens again
                if text:
                    state.number += 1
                    speaker = RtcSpeaker(state.number, whole_numbers=norm is not None)
                    if state.speaker is not None:
                        state.speaker.stop()
                    state.speaker, state.speaking = speaker, state.turn
                    self.create_task(self._answer(text, state.turn, speaker))
            elif isinstance(frame, InterruptionFrame):
                if state.speaker is not None and not state.speaker.stopped:
                    state.speaker.stop()
                    try:
                        state.interrupt(state.speaker.turn)
                    except Exception:
                        log.exception("rtc: interrupting")
                await self.push_frame(frame, direction)
            else:
                await self.push_frame(frame, direction)

        async def _answer(self, text: str, turn: Timeline, speaker: RtcSpeaker):
            state.send({"type": "transcript", "text": text, "language": turn.language, "turn": speaker.turn,
                        "server": turn.ms(), **({"ended": "tap"} if turn.tapped is not None else {})})
            speaker.server = turn.ms()
            work = asyncio.get_running_loop().create_task(answer(text, turn.language or state.language, speaker.turn, speaker))
            work.add_done_callback(_quiet)
            await self.push_frame(LLMFullResponseStartFrame())
            try:
                while (piece := await speaker.queue.get()) is not None:
                    if turn.first_token is None:
                        turn.first_token = time.monotonic()
                    await self.push_frame(AggregatedTextFrame(piece, AggregationType.SENTENCE))
            finally:
                await self.push_frame(LLMFullResponseEndFrame())
                speaker.ended()
            try:
                record = await work if not speaker.stopped else None
            except Exception:
                log.exception("rtc: answering")
                record = None
            if isinstance(record, dict):
                turn.route, turn.asked = record.get("route"), bool(record.get("asked"))

    class KairoTTS(TTSService):
        """Kairo's own voices (Kokoro, Piper); short lines it says often come from the opener cache."""

        def __init__(self):
            super().__init__(push_start_frame=True, push_stop_frames=True, sample_rate=OUT_RATE, stop_frame_timeout_s=10,
                             settings=TTSSettings(model=None, voice=None, language=None))

        async def run_tts(self, text: str, context_id: str):
            turn, number = state.speaking or state.turn, state.number
            if turn.tts_start is None:
                turn.tts_start = time.monotonic()
            lang = state.language if state.language in ("en", "de") else None
            shown = text                                            # g9.speaknorm: said as words, shown as written
            text, lang = norm(text, lang) if norm else (text, lang)
            got = cache.get(text, lang or "en")
            if got is None:
                try:
                    wav, used = await asyncio.to_thread(voice.speak, text, lang)
                except Exception as exc:   # said in the captions; never a crash mid-call
                    log.warning("rtc: voice: %s", exc)
                    state.send({"type": "error", "status": "voice", "message": f"Kairo's voice failed: {exc}"[:300]})
                    return
                pcm, rate = wav_pcm(wav)
                pcm = resample(pcm, rate, self.sample_rate)
                cache.put(text, used, pcm, self.sample_rate)
            else:
                pcm = resample(got[0], got[1], self.sample_rate)
                if turn.first_audio is None:
                    turn.cached = True
            state.send({"type": "say", "text": shown, "turn": number})
            if turn.first_audio is None:
                turn.first_audio = time.monotonic()
                state.send({"type": "timing", "turn": number, "server": turn.ms(), "route": turn.route})
            step = self.sample_rate // 50 * 2                    # 20 ms frames
            for i in range(0, len(pcm), step):
                yield TTSAudioRawFrame(pcm[i:i + step], self.sample_rate, 1, context_id=context_id)

    class Spoken(FrameProcessor):
        """Kairo's voice went quiet at the phone's end of the pipe: the phone may show the answer as over."""

        async def process_frame(self, frame: Frame, direction: FrameDirection):
            await super().process_frame(frame, direction)
            if isinstance(frame, BotStoppedSpeakingFrame):
                state.send({"type": "spoken", "turn": state.number})
            await self.push_frame(frame, direction)

    transport = SmallWebRTCTransport(webrtc_connection=conn,
                                     params=TransportParams(audio_in_enabled=True, audio_out_enabled=True,
                                                            audio_out_10ms_chunks=2))
    stop = TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3(
        smart_turn_model_path=smart_turn_model, params=SmartTurnParams(stop_secs=TURN_MAX_SILENCE_S)))
    pair = LLMContextAggregatorPair(LLMContext(), user_params=LLMUserAggregatorParams(
        user_turn_strategies=UserTurnStrategies(stop=[TapStop(), stop])))
    pipeline = Pipeline([transport.input(), VADProcessor(vad_analyzer=OwnerVAD(params=VADParams(stop_secs=VAD_STOP_S))),
                         Watch(), WhisperSTT(), pair.user(), EndOfTurn(), Answer(), KairoTTS(), transport.output(),
                         Spoken(), pair.assistant()])
    task = PipelineTask(pipeline, params=PipelineParams(enable_metrics=True, audio_out_sample_rate=OUT_RATE))

    @transport.event_handler("on_client_disconnected")
    async def _hung_up(_transport, _client):
        await task.cancel()

    warn = [warning] if warning else []

    @transport.event_handler("on_app_message")
    async def _from_phone(_transport, message, _sender=None):
        """The phone asks Kairo to say a fixed line (a call's opening, or the greeting from the cache): no
        model involved. Or the owner tapped "done": their turn ends now."""
        if not isinstance(message, dict):
            return
        if message.get("type") == "done":
            if state.tap_now():
                log.info("rtc: tapped: the turn ends once Whisper is through")
                await task.queue_frames([Tapped()])
            return
        text = ""
        if message.get("type") == "say":
            from .voicelink import MAX_SAY
            text = str(message.get("text") or "").strip()[:MAX_SAY]
        elif message.get("type") == "greet" and greeting:
            text = greeting
        if text:
            frames = [TTSSpeakFrame(text)]
            if warn:                                 # the voice is off the GPU: said once, after the first line
                frames.append(TTSSpeakFrame(warn.pop()))
            await task.queue_frames(frames)
    call = Call(conn, task, PipelineRunner(handle_sigint=False), state)
    call.openers = cache
    return call


def _quiet(task: asyncio.Task) -> None:
    """An answer that went on after the owner talked over it: its error is logged, never lost."""
    if not task.cancelled() and task.exception() is not None:
        log.warning("rtc: answer: %s: %s", type(task.exception()).__name__, task.exception())


def _last_user_text(context) -> str:
    for m in reversed(getattr(context, "messages", None) or getattr(context, "get_messages", lambda: [])()):
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role != "user":
            continue
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        if isinstance(content, list):
            content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        return str(content or "").strip()
    return ""


