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

import hashlib
import io
import json
import re
import wave
from collections import Counter
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


# ---------------------------------------------------------------- speech-like audio for the mock mode

# First three formants (Hz) of six vowels; a babble of them, moving like syllables, is what Silero hears as speech.
VOWELS = ((730, 1090, 2440), (660, 1720, 2410), (270, 2290, 3010), (390, 1990, 2550), (570, 840, 2410), (300, 870, 2240))
FORMANT_WIDTH = (70.0, 90.0, 130.0)


def _seed(*parts: str) -> int:
    return int.from_bytes(hashlib.sha256("|".join(parts).encode("utf-8")).digest()[:4], "big")


def synth_speech(text: str, rate: int = 16000, variant: str = "") -> "np.ndarray":
    """Speech-like audio for `text`, with no model: about one syllable per three letters, a falling pitch,
    vowel formants that move from syllable to syllable. The same text always gives the same samples, and
    it starts and ends with its first and last sound (no silence around it). Not intelligible, which the
    mock mode doesn't need: its fake speech to text is told what was said."""
    _need_numpy()
    rng = np.random.default_rng(_seed(text, variant))
    words = [w for w in re.findall(r"[\w']+", text)] or ["mm"]
    segs, at = [], 0.0
    for w in words:
        for _ in range(max(1, round(len(w) / 3))):
            dur = float(rng.uniform(0.15, 0.21))
            segs.append((at, at + dur, VOWELS[int(rng.integers(len(VOWELS)))]))
            at += dur + 0.025
        at += 0.06
    total = segs[-1][1]
    n = int(total * rate)
    t = np.arange(n) / rate
    base = float(rng.uniform(105, 190))
    f0 = base * (1.12 - 0.3 * t / total) * (1 + 0.015 * np.sin(2 * np.pi * 5.5 * t))
    phase = 2 * np.pi * np.cumsum(f0) / rate
    formants, amp = np.zeros((3, n)), np.zeros(n)
    for a, b, f in segs:
        sl = slice(int(a * rate), min(n, int(b * rate)))
        size = sl.stop - sl.start
        formants[:, sl] = np.asarray(f)[:, None] * float(rng.uniform(0.94, 1.06))
        amp[sl] = np.hanning(size) ** 0.6
    smooth = np.ones(320) / 320                                   # 20 ms: the formants glide
    formants = np.stack([np.convolve(row, smooth, mode="same") for row in formants])
    k = np.arange(1, int(4000 / f0.min()) + 1)[:, None]
    freq = k * f0[None, :]
    gain = sum(1.0 / (1.0 + ((freq - formants[i][None, :]) / w) ** 2) for i, w in enumerate(FORMANT_WIDTH))
    x = (gain * np.sin(k * phase[None, :]) / np.sqrt(k)).sum(axis=0) * amp
    x = x + 0.01 * np.abs(x).max() * rng.standard_normal(n) * (amp > 0)         # a little breath
    return (x * (0.5 / max(float(np.abs(x).max()), 1e-9))).astype(np.float32)


# ---------------------------------------------------------------- noise (recipes named in the clip ids)

def _burst(rng, n: int, decay: float, rate: int) -> "np.ndarray":
    """White noise that dies away: a click, a clap."""
    return rng.standard_normal(n) * np.exp(-np.arange(n) / (decay * rate))


def _typing(rng, rate: int) -> "np.ndarray":
    x = np.zeros(int(1.6 * rate))
    for at in np.cumsum(rng.uniform(0.07, 0.2, 10)):
        i = int(at * rate)
        if i + int(0.03 * rate) < len(x):
            x[i:i + int(0.03 * rate)] += np.diff(_burst(rng, int(0.03 * rate) + 1, 0.004, rate)) * rng.uniform(0.4, 1.0)
    return x


def _door(rng, rate: int) -> "np.ndarray":
    n = int(0.9 * rate)
    t = np.arange(n) / rate
    return np.sin(2 * np.pi * 70 * t) * np.exp(-t / 0.12) + 0.5 * _burst(rng, n, 0.03, rate)


def _fan(rng, rate: int) -> "np.ndarray":
    n = int(1.8 * rate)
    low = np.convolve(rng.standard_normal(n), np.ones(24) / 24, mode="same")
    return low + 0.3 * np.sin(2 * np.pi * 110 * np.arange(n) / rate)


def _cough(rng, rate: int) -> "np.ndarray":
    out = np.zeros(int(0.9 * rate))
    for start, dur in ((0.05, 0.16), (0.32, 0.14)):
        n = int(dur * rate)
        noise = np.convolve(rng.standard_normal(n), np.ones(4) / 4, mode="same")
        out[int(start * rate):int(start * rate) + n] += noise * np.hanning(n) ** 0.5
    return out


def _clap(rng, rate: int) -> "np.ndarray":
    return _burst(rng, int(0.35 * rate), 0.02, rate)


def _rustle(rng, rate: int) -> "np.ndarray":
    n = int(1.4 * rate)
    env = np.convolve(np.abs(rng.standard_normal(n)), np.ones(rate // 40) / (rate // 40), mode="same")
    return rng.standard_normal(n) * env ** 2


RECIPES = {"typing": _typing, "door": _door, "fan": _fan, "cough": _cough, "clap": _clap, "rustle": _rustle}


def recipe_of(clip: Clip) -> str | None:
    """noise_typing -> "typing". None for a clip that a voice says."""
    return clip.id.removeprefix("noise_") if clip.kind == "noise" and clip.id.startswith("noise_") else None


def synth_noise(clip: Clip, rate: int = 24000) -> "np.ndarray":
    _need_numpy()
    name = recipe_of(clip)
    if name not in RECIPES:
        raise CorpusError(f"{clip.id}: no noise recipe {name!r} (known: {', '.join(RECIPES)})")
    x = RECIPES[name](np.random.default_rng(_seed(clip.id)), rate)
    return (x * (0.35 / max(float(np.abs(x).max()), 1e-9))).astype(np.float32)


# ---------------------------------------------------------------- rendering with Kairo's own voices

# Several voices, so no run depends on one: the English ones are Kokoro's, the owner-like set and a different
# set for strangers. German has one Piper voice installed: speeds differ, and a stranger is that voice with
# its pitch moved.
OWNER_VOICES = {"en": ("af_heart", "am_michael", "af_nicole", "am_adam"), "de": (None,)}
OTHER_VOICES = {"en": ("bm_george", "bf_emma", "am_onyx"), "de": (None,)}
SPEEDS = {"en": (1.0, 0.95, 1.08), "de": (1.0, 0.92, 1.08)}
OTHER_PITCH = {"en": (None,), "de": (0.84, 1.18, 0.9)}
PEAK = 0.5                                              # speech is brought to this peak (-6 dBFS)


def plan(clips: list[Clip]) -> list[tuple[Clip, str | None, float, float | None]]:
    """(clip, Kokoro voice or None, speed, pitch factor or None) for each clip, voices dealt out in turn."""
    count: Counter = Counter()
    out = []
    for c in clips:
        group = (c.lang, c.kind == "other_speaker")
        i = count[group]
        count[group] += 1
        voices = (OTHER_VOICES if group[1] else OWNER_VOICES)[c.lang]
        pitches = OTHER_PITCH[c.lang] if group[1] else (None,)
        out.append((c, voices[i % len(voices)], SPEEDS[c.lang][(i // len(voices)) % 3], pitches[i % len(pitches)]))
    return out


def _say(voice, clip: Clip, name: str | None, speed: float, pitch: float | None) -> tuple["np.ndarray", int, str]:
    from .voicelab import resample, wav_samples
    try:
        wav, _ = voice.speak(clip.text, clip.lang, voice=name, speed=speed)
    except Exception:
        if name is None:
            raise
        wav, _ = voice.speak(clip.text, clip.lang, speed=speed)         # that Kokoro voice isn't in this install
        name = None
    x, rate = wav_samples(wav)
    if pitch:
        x = resample(x, rate, int(rate / pitch))
    label = (name or "default") + (f"@{pitch:g}" if pitch else "")
    return x, rate, label


def _entry(path: Path, x, rate: int, label: str, speed: float, source: str) -> dict:
    return {"file": path.name, "rate": rate, "end_s": speech_end_s(x, rate), "dur_s": round(len(x) / rate, 3),
            "voice": label, "speed": speed, "source": source}


def render(data_dir: Path, voice, clips: list[Clip], *, force: bool = False, say=print) -> dict:
    """Render the clips that aren't there yet (or all with force) into <data>\\bench\\corpus and update its
    index. voice: app.voice.Voice (speak(text, lang, voice=, speed=)). {"rendered": n, "kept": n, "failed": {id: why}}"""
    _need_numpy()
    out = corpus_dir(data_dir)
    index = read_index(data_dir)
    done = {"rendered": 0, "kept": 0, "failed": {}}
    for clip, name, speed, pitch in plan(clips):
        path = out / f"{clip.id}.wav"
        if not force and clip.id in index and path.is_file():
            done["kept"] += 1
            continue
        try:
            if recipe_of(clip):
                x, rate, label = synth_noise(clip), 24000, f"recipe:{recipe_of(clip)}"
            else:
                x, rate, label = _say(voice, clip, name, speed, pitch)
                x = x * (PEAK / max(float(np.abs(x).max()), 1e-9))
            x = np.concatenate([x, np.zeros(int(TRAIL_S * rate), dtype=np.float32)])     # silence after the last voiced sample
            entry = _entry(path, x, rate, label, speed, "recipe" if recipe_of(clip) else "tts")
            write_wav(path, x, rate)
        except Exception as exc:    # one voice that isn't installed doesn't stop the others
            done["failed"][clip.id] = f"{type(exc).__name__}: {exc}"[:200]
            say(f"  {clip.id}: FAILED {done['failed'][clip.id]}")
            continue
        index[clip.id] = entry
        done["rendered"] += 1
        say(f"  {clip.id}: {entry['voice']} {entry['dur_s']:.2f} s, speech ends at {entry['end_s']:.2f} s")
    out.mkdir(parents=True, exist_ok=True)
    tmp = index_path(data_dir).with_suffix(".tmp")
    tmp.write_text(json.dumps({"schema": SCHEMA, "clips": index}, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(index_path(data_dir))
    return done


# ---------------------------------------------------------------- python -m app.bench_corpus render | list

def default_voice(data_dir: Path):
    """Kairo's own voices, the way the app loads them."""
    from . import tool_sites
    from .voice import Voice, models_dir
    site = tool_sites.site("voice", data_dir)
    return Voice(models_dir(data_dir, site), site_dir=site)


def main(argv: list[str] | None = None) -> int:
    import argparse
    from .settings import get_settings
    ap = argparse.ArgumentParser(prog="python -m app.bench_corpus", description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("render", "list"))
    ap.add_argument("--data-dir", type=Path, default=None, help="Kairo's data folder (default: KAIRO_DATA_DIR / devdata)")
    ap.add_argument("--force", action="store_true", help="render every clip again")
    ap.add_argument("--only", default="", help="comma-separated clip ids")
    args = ap.parse_args(argv)
    data_dir = args.data_dir or get_settings().data_dir
    clips = load_manifest()
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        clips = [c for c in clips if c.id in wanted]
    if args.command == "list":
        have = read_index(data_dir)
        for c in clips:
            mine = (own_dir(data_dir) / f"{c.id}.wav").is_file()
            src = "own recording" if mine else (have.get(c.id, {}).get("voice") or "NOT RENDERED")
            print(f"{c.id:14} {c.lang} {c.kind:13} {src:22} {c.text}")
        return 0
    print(f"rendering into {corpus_dir(data_dir)}")
    done = render(data_dir, default_voice(data_dir), clips, force=args.force)
    print(f"{done['rendered']} rendered, {done['kept']} already there, {len(done['failed'])} failed")
    return 1 if done["failed"] else 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
