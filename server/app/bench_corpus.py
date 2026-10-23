"""The clips of the voice latency harness (app/bench_voice.py).

bench/voice/corpus.json is the manifest: 46 short English and German utterances (id, lang, text, kind;
kind = question | statement | command | backchannel | noise | other_speaker). The audio is not in the repo:
`python -m app.bench_corpus render` makes it once with Kairo's own voices (Kokoro English, Piper German,
several voices and speeds) into <data>\\bench\\corpus, with each clip's exact end of speech (its last voiced
sample; silence is added after it). The owner's own recordings, <data>\\bench\\own\\<id>.wav, take
precedence over the rendered clip with the same id (16-bit WAV; their end of speech is found the same way).

Backchannels, noise and other speakers are for the rates: does a "mhm" or a noise stop Kairo while it is
talking (false interrupt), is a stranger's voice taken for the owner (speaker false accept). Noise clips
are made from a recipe named in their id (noise_typing, ...), not by a voice.

The mock mode of the harness needs no models and no recordings: synth_speech() makes speech-like audio
(a source-filter vowel babble that Silero's VAD hears as speech) from the text alone.
"""

from __future__ import annotations

import io
import json
import re
import wave
from dataclasses import dataclass
from pathlib import Path

try:
    import numpy as np                                  # comes with the voice packages
except ImportError:                                     # the app runs without them: no harness
    np = None

from .settings import REPO_ROOT

MANIFEST = REPO_ROOT / "bench" / "voice" / "corpus.json"
SCHEMA = 1
KINDS = ("question", "statement", "command", "backchannel", "noise", "other_speaker")
ANSWERED = ("question", "statement", "command")        # a reply is expected
PROBES = ("backchannel", "noise")                       # Kairo must not stop talking for these
LANGS = ("en", "de")
TRAIL_S = 0.6                                           # silence added after the last voiced sample of a rendered clip
ID = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class CorpusError(ValueError):
    pass


@dataclass(frozen=True)
class Clip:
    id: str
    lang: str
    text: str
    kind: str


@dataclass
class Rendered:
    """A clip ready to play: mono float samples, and where its speech ends (seconds from the start)."""
    clip: Clip
    samples: object                  # np.ndarray, float32 in -1..1
    rate: int
    end_s: float
    source: str                      # "own" (the owner's recording), "tts" (rendered), "synthetic" (mock)
    voice: str = ""
    path: Path | None = None


def _need_numpy() -> None:
    if np is None:
        raise CorpusError("the voice packages (numpy) are not installed")


# ---------------------------------------------------------------- the manifest

def load_manifest(path: Path | None = None) -> list[Clip]:
    path = Path(path) if path else MANIFEST
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise CorpusError(f"corpus manifest {path.name}: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or not isinstance(doc.get("clips"), list):
        raise CorpusError(f"corpus manifest {path.name}: expected {{schema: {SCHEMA}, clips: [...]}}")
    clips, seen = [], set()
    for row in doc["clips"]:
        if not isinstance(row, dict):
            raise CorpusError("corpus manifest: a clip is an object")
        cid, lang, text, kind = (row.get(k) for k in ("id", "lang", "text", "kind"))
        if not isinstance(cid, str) or not ID.match(cid) or cid in seen:
            raise CorpusError(f"corpus manifest: bad or repeated id {cid!r}")
        if lang not in LANGS or kind not in KINDS or not isinstance(text, str) or not text.strip():
            raise CorpusError(f"corpus manifest: {cid}: lang {LANGS}, kind {KINDS}, a text")
        seen.add(cid)
        clips.append(Clip(cid, lang, text.strip(), kind))
    return clips


def select(clips: list[Clip], n: int | None) -> list[Clip]:
    """The first n clips when the kinds are dealt out in turn (so a short run still has every kind, both
    languages), in manifest order. None or n >= len: all of them."""
    if n is None or n >= len(clips):
        return list(clips)
    groups: dict[tuple[str, str], list[Clip]] = {}
    for c in clips:
        groups.setdefault((c.kind, c.lang), []).append(c)
    order = [k for k in sorted(groups, key=lambda k: (KINDS.index(k[0]), LANGS.index(k[1])))]
    picked: list[Clip] = []
    while len(picked) < max(0, n) and any(groups.values()):
        for key in order:
            if groups[key] and len(picked) < n:
                picked.append(groups[key].pop(0))
    keep = {c.id for c in picked}
    return [c for c in clips if c.id in keep]


# ---------------------------------------------------------------- where things are

def bench_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "bench"


def corpus_dir(data_dir: Path) -> Path:
    return bench_dir(data_dir) / "corpus"


def own_dir(data_dir: Path) -> Path:
    return bench_dir(data_dir) / "own"


def results_dir(data_dir: Path) -> Path:
    return bench_dir(data_dir) / "results"


# ---------------------------------------------------------------- audio

def read_wav(path: Path) -> tuple["np.ndarray", int]:
    """Mono float samples and the rate of a 16-bit WAV (stereo: the channels averaged)."""
    _need_numpy()
    from .voicelab import wav_samples
    data = Path(path).read_bytes()
    try:
        with wave.open(io.BytesIO(data)) as w:
            channels, width = w.getnchannels(), w.getsampwidth()
    except (wave.Error, EOFError) as exc:
        raise CorpusError(f"{Path(path).name}: not a WAV ({exc})") from exc
    if width != 2:
        raise CorpusError(f"{Path(path).name}: 16-bit WAV please ({width * 8}-bit)")
    x, rate = wav_samples(data)
    if channels > 1:
        x = x[: len(x) // channels * channels].reshape(-1, channels).mean(axis=1).astype(np.float32)
    return x, rate


def write_wav(path: Path, samples, rate: int) -> None:
    _need_numpy()
    from .voicelab import to_pcm16
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(to_pcm16(samples))
    tmp.replace(path)


def speech_end_s(samples, rate: int) -> float:
    """Where the speech in these samples ends: the last voiced sample, in seconds from the start. Voiced is
    what the speaker check calls voiced (speaker.level: a 20 ms frame within 30 dB of the loudest and not
    quieter than -60 dBFS); inside the last such frame, the last sample above a tenth of its peak. 0.0 for
    silence."""
    _need_numpy()
    from . import speaker
    from .voicelab import resample
    x = resample(np.asarray(samples, dtype=np.float32), rate, speaker.RATE)
    voiced = np.flatnonzero(np.asarray(speaker.level(x).voiced, dtype=bool))
    if not len(voiced):
        return 0.0
    first = int(voiced[-1]) * speaker.LEVEL_FRAME
    frame = np.abs(x[first:first + speaker.LEVEL_FRAME])
    loud = np.flatnonzero(frame >= 0.1 * frame.max()) if frame.size and frame.max() > 0 else []
    last = first + (int(loud[-1]) + 1 if len(loud) else len(frame))
    return round(last / speaker.RATE, 4)


# ---------------------------------------------------------------- the rendered and the owner's clips

def index_path(data_dir: Path) -> Path:
    return corpus_dir(data_dir) / "index.json"


def read_index(data_dir: Path) -> dict:
    try:
        doc = json.loads(index_path(data_dir).read_text(encoding="utf-8"))
        return doc["clips"] if doc.get("schema") == SCHEMA and isinstance(doc.get("clips"), dict) else {}
    except (OSError, ValueError, KeyError, AttributeError):
        return {}


def load(data_dir: Path, clip: Clip) -> Rendered | None:
    """The clip to play: the owner's recording if there is one, else the rendered one, else None."""
    own = own_dir(data_dir) / f"{clip.id}.wav"
    if own.is_file():
        x, rate = read_wav(own)
        return Rendered(clip, x, rate, speech_end_s(x, rate), "own", "owner", own)
    entry = read_index(data_dir).get(clip.id)
    path = corpus_dir(data_dir) / f"{clip.id}.wav"
    if not entry or not path.is_file():
        return None
    x, rate = read_wav(path)
    end = entry.get("end_s")
    return Rendered(clip, x, rate, float(end) if isinstance(end, (int, float)) else speech_end_s(x, rate), "tts",
                    str(entry.get("voice") or ""), path)


def missing(data_dir: Path, clips: list[Clip]) -> list[Clip]:
    """Clips that have neither a recording of the owner's nor a rendered file."""
    have = read_index(data_dir)
    return [c for c in clips if not (own_dir(data_dir) / f"{c.id}.wav").is_file()
            and not (c.id in have and (corpus_dir(data_dir) / f"{c.id}.wav").is_file())]
