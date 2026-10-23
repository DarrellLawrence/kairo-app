"""Voice V1 (2026-10-15): a spoken turn over one WebSocket, both ways (/ws/voice).

The phone streams 16 kHz 16-bit mono PCM while the owner talks; its own voice detection decides
when they talk and when they stopped (about half a second of quiet). At a shorter pause the PC
already transcribes what it has, so at the end of speech the words are usually ready. The answer
comes back as text and as audio, one sentence at a time: the PC makes sentence 2's audio while
the phone plays sentence 1.

Client -> server
  {"type": "start", "rate": 16000}        a new utterance (throws away an unfinished one)
  binary                                  PCM16 LE mono at that rate, added to the utterance
  {"type": "pause"}                       short quiet: transcribe what there is (kept if nothing follows);
                                          with smart endpointing the PC also decides whether the turn is over
  {"type": "end", "path": "call"|"chat_voice", "call_id": 3|null, "turn": n}
                                          end of speech: transcribe, answer, speak
  {"type": "cancel"}                      throw the utterance away
  {"type": "stop"}                        the owner talked over Kairo: stop the answer and the voice now
  {"type": "say", "text": "...", "turn": n}   speak this (a greeting); no model involved
Server -> client (every frame of a turn carries its "turn" number: the phone drops a talked-over
turn's late frames)
  {"type": "hello", "endpoint": "smart", "pause_ms": 200, "end_ms": 1500}
                                          smart endpointing (2026-10-16): the phone sends "pause" after 200 ms
                                          of quiet and only ends a turn itself after 1.5 s; the PC decides
  {"type": "endpoint", "turn": n, "p": 0.9, "ms": 25}   the PC ended the turn (n is its number): answer
  {"type": "transcript", "text", "language", "stt_ms"}    ("" = nothing understood)
  {"type": "thread" | "delta" | "model" | "done" | "error", ...}   as /ws/chat
  {"type": "sentence", "i": n}            sentence n is written; its audio is being made
  {"type": "say", "i": n, "text": "..."}  the sentence whose audio is the next binary frame
  binary                                  audio: uint32 LE sample rate, turn, sentence number; PCM16 LE mono
  {"type": "spoken", "n": count}          every sentence of this answer was sent
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import struct
import time
import wave

log = logging.getLogger(__name__)

RATE = 16000                  # what the phone sends and Whisper wants
MAX_SECONDS = 90              # one utterance; longer is cut (a voice note is the place for that)
MAX_BYTES = RATE * 2 * MAX_SECONDS
HEADER = struct.Struct("<III")  # sample rate, turn, sentence number

# A period here doesn't end a sentence: "z.B.", "Dr.", "3. Oktober" (German dates), "No. 5".
_NOT_AN_END = re.compile(r"(?:^|[\s(])(?:dr|mr|mrs|ms|st|nr|no|bzw|ca|vs|etc|usw|inkl|evtl|ggf|z\.b|u\.a|d\.h|e\.g|i\.e)\.$|\d\.$",
                         re.I)
# The end of a sentence: . ! ? … (and closing quotes or brackets) before a space, or a line break.
_END = re.compile(r"[.!?…]+[\"'”’»)\]]*(?=\s)|\n+")
# With g9.speaknorm: "Fr." (francs or Friday) and the weekday abbreviations stand in front of the number they explain
# ("Fr. 12.50", "Mo. 23.10."), so their dot is no place to cut.
_BEFORE_NUMBER = re.compile(r"(?:^|[\s(])(?:S?Fr|Mo|Di|Mi|Do|Sa|So)\.$")
FIRST_MIN = 12   # the first piece starts the voice: as soon as there is a short sentence
NEXT_MIN = 40    # later pieces are made while the one before plays: fewer, longer ones
LONG = 220       # no sentence end for this long: cut at a comma (or a space) so the voice doesn't wait
HANG_UP = re.compile(r"\s*\[\[hang up\]\]\s*", re.I)   # a learning call's last turn; never spoken
MAX_SAY = 600    # a greeting spoken without the model
# Owner's decision 2026-10-17: a voice off the GPU never blocks a call; it is said once when the call starts
# (after the greeting or the call's opening), and the reason goes to the phone as a push.
SLOW_LINE = "Heads up: my voice is slow right now, there's a GPU problem. I've sent you the details."
# What Whisper "hears" in a breath or room noise: subtitle credits from its training data. Never the owner.
_PHANTOM = re.compile(r"untertitel|amara\.org|subtitles by|captions by|copyright ©|\bzdf\b|swr 20\d\d", re.I)


FIRST_WORDS = 5  # owner's request 2026-10-16/17: the voice starts after the first clause or 5 words


def first_clause(text: str, words: int = FIRST_WORDS, whole_numbers: bool = False) -> int | None:
    """Where the first piece of a reply may go to the voice while it is still being written: just after its
    first comma / colon / semicolon / sentence end, or after `words` words. None: keep collecting. Only
    with a space after it (in "at 7.42" the "7." isn't an end yet), and never at "Dr." or "3." (a date).
    whole_numbers (g9.speaknorm): the voice's number rules see one piece at a time, so a word cut at
    five words is not cut off from its number: not after a word with a digit ("14:30" keeps its "Uhr",
    "12,50" its "Franken"), and not in front of a word that starts with one ("CHF | 120.50", "am | 23.10.",
    "in | 1 Minute"). It waits until the next word has begun to arrive."""
    for m in re.finditer(r"[,;:.!?](?=\s)|\s", text):
        piece = text[:m.end()].strip()
        if not piece:
            continue
        if m.group(0) in ",;:!?":
            return m.end()
        if m.group(0) == "." and not _NOT_AN_END.search(text[max(0, m.start() - 6):m.start() + 1]) \
                and not (whole_numbers and _BEFORE_NUMBER.search(text[max(0, m.start() - 4):m.start() + 1])):
            return m.end()
        if len(piece.split()) >= words and m.group(0).isspace():
            if not whole_numbers:
                return m.end()
            ahead = text[m.end():].lstrip()
            if ahead and not ahead[0].isdigit() and not re.search(r"\d", piece.split()[-1]):
                return m.end()
    return None


class Sentences:
    """Cuts an answer into pieces to speak while it is still being written: the first at its first clause
    or 5 words (the voice starts at once), then whole sentences."""

    def __init__(self, whole_numbers: bool = False) -> None:
        self.buf = ""
        self.count = 0
        self.whole_numbers = whole_numbers

    def feed(self, delta: str) -> list[str]:
        self.buf += delta or ""
        out, start = [], 0
        if self.count == 0:
            at = first_clause(self.buf, whole_numbers=self.whole_numbers)
            if at is None:
                return []
            out.append(self.buf[:at].strip())
            start = at
        for m in _END.finditer(self.buf, start):
            end = m.end()
            if m.group(0)[0] == "." and _NOT_AN_END.search(self.buf[max(start, m.start() - 6):m.start() + 1]):
                continue
            piece = self.buf[start:end].strip()
            if len(piece) < (FIRST_MIN if self.count + len(out) == 0 else NEXT_MIN):
                continue
            out.append(piece)
            start = end
        self.buf = self.buf[start:]
        if len(self.buf) > LONG:
            cut = self.buf.rfind(", ", 0, LONG)
            if cut < 60:
                cut = self.buf.rfind(" ", 0, LONG)
            if cut > 0:
                out.append(self.buf[:cut + 1].strip())
                self.buf = self.buf[cut + 1:]
        self.count += len(out)
        return [p for p in (speakable(x) for x in out) if p]

    def flush(self) -> str:
        rest, self.buf = speakable(self.buf), ""
        if rest:
            self.count += 1
        return rest


def heard(text: str) -> str:
    """The owner's words, or "" when the transcript is only noise."""
    t = re.sub(r"\s+", " ", text or "").strip()
    return "" if not t or (len(t) < 120 and _PHANTOM.search(t)) else t


def speakable(text: str) -> str:
    return HANG_UP.sub(" ", text or "").strip()


def frame(rate: int, turn: int, index: int, pcm: bytes) -> bytes:
    return HEADER.pack(rate, turn, index) + pcm


def wav_pcm(wav: bytes) -> tuple[bytes, int]:
    """(PCM16 mono samples, sample rate) from the WAV that Voice.speak makes."""
    with wave.open(io.BytesIO(wav)) as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError("expected 16-bit mono audio")
        return w.readframes(w.getnframes()), w.getframerate()


def filler(language: str) -> str:
    """Said while the stronger model takes over a turn."""
    return "Einen Moment." if language == "de" else "One moment."


class Utterance:
    """What the owner is saying now, and a transcript made at a pause (used if nothing came after)."""

    def __init__(self, path: str = "call", call_id=None) -> None:
        self.pcm = bytearray()
        self.early: tuple[int, asyncio.Task] | None = None
        self.started = time.monotonic()
        self.path, self.call_id = path, call_id
        self.version = 0          # +1 with every piece of audio: a pause that was followed by more words is stale

    def add(self, data: bytes) -> bool:
        if len(self.pcm) + len(data) > MAX_BYTES:
            return False
        self.pcm.extend(data)
        self.version += 1
        return True

    @property
    def seconds(self) -> float:
        return len(self.pcm) / (RATE * 2)


class Speaker:
    """Speaks an answer sentence by sentence: audio for the next sentence is made while the
    phone plays the one before. stop() ends it at once (talked over, hung up)."""

    def __init__(self, voice, send_json, send_bytes, language: str | None, turn: int = 0, norm=None) -> None:
        self.voice, self.send_json, self.send_bytes, self.turn = voice, send_json, send_bytes, turn
        self.language = language if language in ("en", "de") else None
        self.norm = norm                    # g9.speaknorm: (text, language) -> (what the voice says, language), or None
        self.split = Sentences(norm is not None)
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent = 0
        self.first_audio_ms: int | None = None
        self.tts_ms: list[int] = []
        self.failure: str | None = None
        self.started = time.monotonic()
        self.closed = False
        self.task = asyncio.get_running_loop().create_task(self._run())

    def feed(self, delta: str) -> None:
        for piece in self.split.feed(delta):
            self.queue.put_nowait(piece)

    def say(self, text: str) -> None:
        text = speakable(text)
        if text:
            self.queue.put_nowait(text)

    def finish(self, final_text: str | None = None) -> None:
        """The answer is complete. final_text: the whole answer, when nothing came in pieces."""
        if self.closed:
            return
        self.closed = True
        rest = self.split.flush()
        if rest:
            self.queue.put_nowait(rest)
        elif final_text and self.split.count == 0 and self.sent == 0 and self.queue.empty():
            self.say(final_text)
        self.queue.put_nowait(None)

    def stop(self) -> None:
        self.task.cancel()

    async def done(self) -> None:
        try:
            await self.task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        from .voice import language_of
        heard = []
        while (text := await self.queue.get()) is not None:
            heard.append(text)
            lang = self.language or language_of(" ".join(heard))
            started = time.monotonic()
            spoken, lang = self.norm(text, lang) if self.norm else (text, lang)   # g9.speaknorm: said as words, shown as written
            await self.send_json({"type": "sentence", "i": self.sent, "turn": self.turn})   # written; its voice is being made
            try:
                wav, _used = await asyncio.to_thread(self.voice.speak, spoken, lang)
                pcm, rate = wav_pcm(wav)
            except asyncio.CancelledError:
                raise
            except Exception as exc:   # one sentence without a voice: say why once, keep the text going
                log.warning("voice: a sentence could not be spoken: %s", exc)
                if self.failure is None:
                    self.failure = f"{type(exc).__name__}: {exc}"[:200]
                    await self.send_json({"type": "error", "status": "voice", "message": f"Kairo's voice failed: {exc}",
                                          "turn": self.turn})
                continue
            self.tts_ms.append(int((time.monotonic() - started) * 1000))
            await self.send_json({"type": "say", "i": self.sent, "text": text, "turn": self.turn})
            await self.send_bytes(frame(rate, self.turn, self.sent, pcm))
            if self.first_audio_ms is None:
                self.first_audio_ms = int((time.monotonic() - self.started) * 1000)
            self.sent += 1
        await self.send_json({"type": "spoken", "n": self.sent, "turn": self.turn})
