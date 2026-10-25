"""Only the owner is answered on calls (owner's approval 2026-10-20: one model file, no new package).

Two checks on every finished turn of a call, before anything answers it:

1. Too quiet / far (no model): the turn's speech level (RMS and peak in dBFS over its voiced frames)
   against the owner's own level on this call (the median of the last answered turns). A turn far below
   both is someone across the room or the TV: ignored.
2. Not you (once the owner taught Kairo their voice, More -> Settings -> Voice): WeSpeaker's CAM++
   (English, VoxCeleb) as sherpa-onnx publishes it turns the turn's audio into 512 numbers; their cosine
   similarity to the owner's voiceprint below the threshold: ignored. Short turns (under SHORT_S of
   speech) are only ignored when the score says clearly someone else (too little audio to be sure).

An ignored turn gets no reply, no Claude, no bring-up; the call keeps listening. Its words are never
logged or kept: only the time, the reason, the score and the level (Voice speed, Run diagnosis).

This is a filter for the room, not a security boundary (whatever reaches another person is an approval
card, gate.py), so it fails open: model missing, an error or a slow check = the turn is answered.

The features are Kaldi's 80-dim log mel filterbank the way WeSpeaker makes them for its models
(torchaudio.compliance.kaldi.fbank with window_type="hamming": 25 ms frames every 10 ms, no dither, DC
removed, pre-emphasis 0.97, 512-point power spectrum, 80 triangular mel bins 20 Hz - 8 kHz, log floored
at float eps, samples at int16 scale), then the mean over time subtracted (CMN: WeSpeaker does it before
the model, the ONNX file doesn't). Made with numpy here, so the voice packages need nothing new. The
model runs on ONNX Runtime with Kokoro's choice (CUDA on the PC, else the CPU, and Voice speed says why),
in a thread next to Whisper, so a turn waits for it only a few ms.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import json
import logging
import statistics
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

try:
    import numpy as np                                  # comes with the voice packages
except ImportError:                                     # the app runs without them: the checks are off
    np = None

from .speaker_label import LABELS, OWNER, STRANGER, UNCHECKED, UNSURE, worst   # noqa: F401  (LABELS: tests and callers)

log = logging.getLogger("kairo.speaker")

MODEL_NAME = "wespeaker_en_voxceleb_CAM++.onnx"         # the pin (URL, sha256, size) is in voicellm.json

# ---------------------------------------------------------------- every number in one place

RATE = 16000
FRAME_LEN, FRAME_SHIFT, N_FFT, N_MELS = 400, 160, 512, 80    # 25 ms / 10 ms at 16 kHz, Kaldi's padded FFT
PREEMPH, LOW_HZ, HIGH_HZ = 0.97, 20.0, 8000.0
EPS = 1.1920929e-07                                           # float32 eps: Kaldi's floor before the log

LEVEL_FRAME = 320                    # 20 ms frames for the speech level
VOICED_BELOW_PEAK_DB = 30.0          # a frame within 30 dB of the turn's loudest one is speech ...
SILENCE_DBFS = -60.0                 # ... and never one below -60 dBFS

MODES = ("strict", "normal", "off")
DEFAULT_MODE = "normal"
# Cosine similarity to the voiceprint: below this, not the owner. Measured 2026-10-20 with this frontend on
# LibriSpeech test-clean (38 readers, 30 s voiceprints, crops of another recording): the owner's turns of
# 1.5 s score 0.42 or more (1st percentile), 0.74 typically; someone else 0.11 typically, 0.39 at the 99th
# percentile. Clean audio; the phone and the room cost some of that margin, so the defaults lean towards
# answering the owner: normal ignores about 1 in 100 of the owner's 1-2 s turns and lets about 1 in 20 of
# someone else's through, strict about 2 in 100 and 1 in 200.
THRESHOLD = {"strict": 0.40, "normal": 0.30}
SHORT_S = 0.8                        # under this much speech a weak score alone never ignores a turn ...
SHORT_REJECT = {"strict": 0.12, "normal": 0.05}   # ... only a score this low: clearly someone else
MIN_CHECK_S = 0.3                    # under this, no voiceprint at all: answered (too little to tell)

QUIET_DROP_DB = 20.0                 # 20 dB (10x) under the owner's level on this call, RMS and peak: too far
QUIET_REF_TURNS = 5                  # the owner's level: median of the last 5 answered turns of the call ...
QUIET_MIN_REF = 2                    # ... once there are 2 (before that nothing is too quiet)
QUIET_RESET_AFTER = 3                # 3 too-quiet turns in a row: the level is learned again (the owner moved)

WAIT_MAX_S = 0.3                     # a turn waits at most this for the check, then it is answered
CHECK_FRAMES = (137, 299)            # the load check's lengths: not multiples of 100 (see SpeakerModel._load)
ENROLL_MIN_S, ENROLL_MAX_S = 10.0, 60.0           # seconds of speech for a voiceprint (the phone records 30 s)
ENROLL_WINDOW_S, ENROLL_HOP_S = 3.0, 1.5          # the voiceprint: mean of 3 s windows, every 1.5 s
# One voice: every window's median similarity to the others is 0.50 or more (LibriSpeech, 20 readers);
# half the time another voice: under 60% of the windows agree. So 3 in 4 windows must agree, and the
# voiceprint is the mean of those that do (a short second voice is left out).
ENROLL_AGREE, ENROLL_MIN_KEPT = 0.45, 0.75
MAX_ENROLL_BYTES = int(ENROLL_MAX_S * 1.5 * RATE * 2)   # 90 s of 16-bit audio (pauses included)
VOICEPRINT = Path("voice") / "voiceprint.bin"     # in the data folder (encrypted: secretbox), never the repo
MAX_WAITING_LABELS = 16              # G13: verdicts waiting for their answer to start; more than this and the oldest merge
KEEP_TURNS = 500                     # Voice speed's list (time, reason, score; never the words)
STATS_KEY = "voice.speaker.turns"
MODE_KEY = "voice.speaker.mode"
REASONS = {"not_you": "ignored: not you", "too_quiet": "ignored: too quiet / far"}


class SpeakerError(Exception):
    """Something the owner can fix (say more, somewhere quiet)."""


class NotReady(SpeakerError):
    """The PC can't do it yet (model or voice packages missing)."""


# ---------------------------------------------------------------- the features (numpy)

def _mel(f):
    return 1127.0 * np.log(1.0 + np.asarray(f, dtype=np.float64) / 700.0)


def mel_banks(n_mels: int = N_MELS, n_fft: int = N_FFT, rate: int = RATE, low: float = LOW_HZ,
              high: float = HIGH_HZ) -> np.ndarray:
    """(n_mels, n_fft // 2 + 1) Kaldi mel triangles (in mel, not normalised); the Nyquist bin gets 0."""
    lo, hi = _mel(low), _mel(high)
    delta = (hi - lo) / (n_mels + 1)
    left = lo + delta * np.arange(n_mels)[:, None]
    center, right = left + delta, left + 2 * delta
    mel = _mel(rate / n_fft * np.arange(n_fft // 2))[None, :]
    up, down = (mel - left) / (center - left), (right - mel) / (right - center)
    banks = np.maximum(0.0, np.minimum(up, down))
    return np.pad(banks, ((0, 0), (0, 1))).astype(np.float32)


_CONST: dict = {}


def _const():
    if "window" not in _CONST:                          # set last: once it's there, both are
        _CONST["banks"] = mel_banks()
        n = np.arange(FRAME_LEN)
        _CONST["window"] = (0.54 - 0.46 * np.cos(2 * np.pi * n / (FRAME_LEN - 1))).astype(np.float32)
    return _CONST["banks"], _CONST["window"]


def frame_count(n_samples: int) -> int:
    """Kaldi's snip_edges: only whole 25 ms frames."""
    return 0 if n_samples < FRAME_LEN else 1 + (n_samples - FRAME_LEN) // FRAME_SHIFT


def fbank(samples) -> np.ndarray:
    """(frames, 80) float32 log mel energies of 16 kHz float audio (-1..1), before the mean is taken off."""
    x = np.asarray(samples, dtype=np.float32) * 32768.0          # WeSpeaker reads samples at int16 scale
    n = frame_count(len(x))
    if n == 0:
        return np.zeros((0, N_MELS), dtype=np.float32)
    idx = np.arange(FRAME_LEN)[None, :] + FRAME_SHIFT * np.arange(n)[:, None]
    frames = x[idx]
    frames = frames - frames.mean(axis=1, keepdims=True)         # remove the DC offset
    frames = frames - PREEMPH * np.concatenate([frames[:, :1], frames[:, :-1]], axis=1)
    banks, window = _const()
    power = np.abs(np.fft.rfft(frames * window, n=N_FFT, axis=1)).astype(np.float32) ** 2
    return np.log(np.maximum(power @ banks.T, EPS)).astype(np.float32)


def features(samples) -> np.ndarray:
    """What the model reads: (frames, 80), the mean over time subtracted (CMN)."""
    f = fbank(samples)
    return f - f.mean(axis=0, keepdims=True) if len(f) else f


def pcm16(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0


# ---------------------------------------------------------------- the speech level (no model)

@dataclass
class Level:
    speech_s: float                     # seconds of voiced frames
    rms_db: float | None                # RMS over the voiced frames, dBFS
    peak_db: float | None               # loudest sample, dBFS
    voiced: object = None               # bool per 20 ms frame (np.ndarray)
    power: float = 0.0                  # mean power over the voiced frames (to add turns up)
    frames: int = 0


def _db(power: float) -> float:
    return round(10 * float(np.log10(max(power, 1e-12))), 1)


def level(samples) -> Level:
    x = np.asarray(samples, dtype=np.float32)
    n = len(x) // LEVEL_FRAME
    if n == 0:
        return Level(0.0, None, None, np.zeros(0, dtype=bool))
    p = (x[: n * LEVEL_FRAME].reshape(n, LEVEL_FRAME).astype(np.float64) ** 2).mean(axis=1)
    db = 10 * np.log10(np.maximum(p, 1e-12))
    voiced = (db >= db.max() - VOICED_BELOW_PEAK_DB) & (db >= SILENCE_DBFS)
    if not voiced.any():
        return Level(0.0, None, None, voiced)
    power = float(p[voiced].mean())
    peak = float(np.abs(x).max())
    return Level(round(float(voiced.sum()) * LEVEL_FRAME / RATE, 2), _db(power), _db(peak * peak), voiced,
                 power, int(voiced.sum()))


def voiced_audio(samples, lv: Level, keep: int = 5) -> np.ndarray:
    """The voiced frames with up to `keep` frames (100 ms) around them; long quiet stretches dropped."""
    x = np.asarray(samples, dtype=np.float32)
    v = np.asarray(lv.voiced, dtype=bool)
    if not v.any():
        return x[:0]
    near = np.convolve(v.astype(np.int32), np.ones(2 * keep + 1, dtype=np.int32), mode="same") > 0
    return x[: len(near) * LEVEL_FRAME].reshape(len(near), LEVEL_FRAME)[near].reshape(-1)


# ---------------------------------------------------------------- the decision (pure, tested)

def speaker_verdict(score: float | None, speech_s: float, mode: str) -> tuple[bool, str]:
    """(answer?, why) from the cosine score alone. Short turns: only clearly someone else is ignored."""
    if mode not in THRESHOLD:
        return True, "off"
    if score is None:
        return True, "no score"
    if speech_s < MIN_CHECK_S:
        return True, "too short to tell"
    if speech_s < SHORT_S:
        return (score >= SHORT_REJECT[mode]), ("short: you" if score >= SHORT_REJECT[mode] else "not_you")
    return (score >= THRESHOLD[mode]), ("you" if score >= THRESHOLD[mode] else "not_you")


def label_for(*, on: bool, ok: bool, score: float | None, mode: str) -> str:
    """G13: the label of a turn (speaker_label.py). on: the check runs (a voiceprint, the model, not switched off).
    ok: the turn passed the check (speaker_verdict). Only a score at or over the threshold is the owner; a short
    turn that passed with a weaker score, or any turn the check could not score, is unsure."""
    if not on:
        return UNCHECKED
    if score is None:
        return UNSURE
    if not ok:
        return STRANGER
    return OWNER if score >= THRESHOLD.get(mode, 2.0) else UNSURE


def too_quiet(rms_db: float | None, peak_db: float | None, owner: list[tuple[float, float]]) -> bool:
    """Far below the owner's own level on this call (both RMS and peak). Needs QUIET_MIN_REF answered turns."""
    if rms_db is None or peak_db is None or len(owner) < QUIET_MIN_REF:
        return False
    ref_rms = statistics.median(r for r, _ in owner[-QUIET_REF_TURNS:])
    ref_peak = statistics.median(p for _, p in owner[-QUIET_REF_TURNS:])
    return rms_db < ref_rms - QUIET_DROP_DB and peak_db < ref_peak - QUIET_DROP_DB


def cosine(a, b) -> float:
    a, b = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    return float(a @ b / max(float(np.linalg.norm(a) * np.linalg.norm(b)), 1e-9))


def _unit(y) -> np.ndarray:
    y = np.asarray(y, dtype=np.float32).reshape(-1)
    return y / max(float(np.linalg.norm(y)), 1e-9)


# ---------------------------------------------------------------- the model (ONNX Runtime, Kokoro's provider choice)

class SpeakerModel:
    """embed(16 kHz float audio) -> 512 numbers (unit length). The file comes with the installer
    (voicellm.json -> tools\\voicellm-<hash>\\speaker); without it the speaker check is off."""

    def __init__(self, path=None, *, site_dir: Path | None = None, session=None):
        """path: the model file, or a function that finds it (it can arrive after Kairo started)."""
        self._path, self.site_dir, self._session = path, site_dir, session
        self._lock = threading.Lock()
        self.device: str | None = "cpu" if session is not None else None
        self.why: str | None = None          # why not on the GPU
        self.checked: str | None = None      # a way of running it that gave wrong voiceprints (and was skipped)
        self.failed: str | None = None
        self.load_ms: int | None = None
        self.warm_ms: int | None = None

    @property
    def path(self) -> Path | None:
        p = self._path() if callable(self._path) else self._path
        return Path(p) if p else None

    def installed(self) -> bool:
        return np is not None and (self._session is not None or (self.path is not None and self.path.is_file()))

    @property
    def loaded(self) -> bool:
        return self._session is not None

    def _open(self, ort, providers, optimize: bool):
        opts = ort.SessionOptions()
        if not optimize:
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
        return ort.InferenceSession(str(self.path), sess_options=opts, providers=providers)

    def _load(self):
        """Kokoro's provider choice, then a check: ONNX Runtime 1.24 (the PC's, pinned for Pipecat) computes this
        model wrong with its graph optimizations on, for any length that isn't a multiple of 100 frames (found
        2026-10-20; 1.31 is right). So each candidate session must give the plain CPU session's voiceprints for
        two odd lengths, or the next one is tried: CUDA optimised, CUDA plain, CPU optimised, CPU plain."""
        with self._lock:
            if self._session is None:
                if not self.installed():
                    raise NotReady("the speaker check's model is not on the PC yet (run Update Kairo)")
                import onnxruntime as ort                    # the voice packages' (the CUDA build on the PC)

                from .voice import _ort_cuda
                t0 = time.monotonic()
                providers, why = _ort_cuda(self.site_dir)
                cpu = ["CPUExecutionProvider"]
                plain = self._open(ort, cpu, False)
                name = plain.get_inputs()[0].name
                probes = [np.random.default_rng(7).standard_normal((1, n, N_MELS)).astype(np.float32) for n in CHECK_FRAMES]
                want = [_unit(plain.run(None, {name: p})[0]) for p in probes]
                session, wrong = plain, []
                tries = [(providers, True), (providers, False)] if providers != cpu else []
                for prov, optimize in tries + [(cpu, True)]:
                    try:
                        s = self._open(ort, prov, optimize)
                    except Exception as exc:   # CUDA there but broken: the CPU, and it says why
                        why = f"{type(exc).__name__}: {exc}"[:300]
                        continue
                    if all(float(_unit(s.run(None, {name: p})[0]) @ w) >= 0.999 for p, w in zip(probes, want)):
                        session = s
                        break
                    wrong.append(f"{s.get_providers()[0].removesuffix('ExecutionProvider')}{' optimised' if optimize else ''}")
                on_gpu = session.get_providers()[0] == "CUDAExecutionProvider"
                if not on_gpu and why is None:
                    why = ("ONNX Runtime could not start CUDA (CUDA/cuDNN DLLs not found)" if not wrong else
                           f"ONNX Runtime {ort.__version__} computes this model wrong on the GPU")
                self.checked = (f"ONNX Runtime {ort.__version__} got it wrong as {', '.join(wrong)}: using the next way"
                                if wrong else None)
                if wrong:
                    log.warning("speaker check: %s", self.checked)
                self.device, self.why = ("cuda" if on_gpu else "cpu"), (None if on_gpu else why)
                self._session = session
                self.load_ms = int((time.monotonic() - t0) * 1000)
        return self._session

    def embed(self, samples):
        """Unit-length np.ndarray, or None for too little audio. Raises when the model can't run."""
        f = features(samples)
        if len(f) < int(MIN_CHECK_S * 100) - 2:
            return None
        sess = self._load()
        out = np.asarray(sess.run(None, {sess.get_inputs()[0].name: f[None]})[0], dtype=np.float32).reshape(-1)
        n = float(np.linalg.norm(out))
        if not np.isfinite(out).all() or n == 0:
            return None
        return out / n

    def warm(self) -> None:
        """Load once and run once (CUDA kernels), so no turn waits for it."""
        try:
            t0 = time.monotonic()
            self.embed((np.random.default_rng(0).standard_normal(RATE * 2) * 0.05).astype(np.float32))
            self.warm_ms = int((time.monotonic() - t0) * 1000)
        except Exception as exc:   # said in Voice speed; the check stays off
            self.failed = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("speaker check: the model could not start: %s", self.failed)


# ---------------------------------------------------------------- the owner's voiceprint and the setting

class Speaker:
    """App-wide: the model, the owner's voiceprint (encrypted in the data folder, never the repo), the
    setting (strict / normal / off) and the per-turn record. gate() makes one call's checker."""

    def __init__(self, model: SpeakerModel, store: Path, engine=None, *, now=None):
        self.model, self.store, self.engine = model, Path(store), engine
        self.now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self._print: tuple[float, dict | None] | None = None      # (mtime, the stored voiceprint)
        self._mode: tuple[float, str] | None = None               # (when read, the setting)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="speaker")
        self.writer = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="speaker-record")

    # the voiceprint
    def _load_print(self) -> dict | None:
        from . import secretbox
        try:
            mtime = self.store.stat().st_mtime
        except OSError:
            self._print = None
            return None
        if self._print is None or self._print[0] != mtime:
            try:
                v = secretbox.load(self.store)
            except Exception as exc:   # unreadable (another account, a copy from another PC): off, and said
                log.warning("speaker check: the voiceprint can't be read: %s", type(exc).__name__)
                v = {"error": "the voiceprint can't be read on this PC: teach Kairo your voice again"}
            self._print = (mtime, v if isinstance(v, dict) else None)
        return self._print[1]

    def voiceprint(self):
        v = self._load_print()
        if not v or np is None or v.get("model") != MODEL_NAME or not isinstance(v.get("vector"), list):
            return None
        return np.asarray(v["vector"], dtype=np.float32)

    def enrolled(self) -> dict | None:
        """What the app shows (never the numbers themselves)."""
        v = self._load_print()
        if not v:
            return None
        out = {k: v.get(k) for k in ("at", "speech_s", "windows", "kept", "agreement")}
        if v.get("error"):
            out["problem"] = v["error"]
        elif v.get("model") != MODEL_NAME:
            out["problem"] = "made with another model: teach Kairo your voice again"
        return out

    def remove(self) -> bool:
        from . import secretbox
        self._print = None
        return secretbox.remove(self.store)

    def enroll(self, pcm: bytes) -> dict:
        """The owner read aloud for about 30 s (16 kHz 16-bit mono). Raises SpeakerError with what to do."""
        from . import secretbox
        if np is None:
            raise NotReady("the voice packages are not installed on the PC")
        if not self.model.installed():
            raise NotReady("the speaker check's model is not on the PC yet (run Update Kairo)")
        if len(pcm) > MAX_ENROLL_BYTES:
            raise SpeakerError(f"that recording is too long (at most {int(ENROLL_MAX_S)} s of speech)")
        x = pcm16(pcm)
        lv = level(x)
        if lv.speech_s < ENROLL_MIN_S:
            raise SpeakerError(f"only {lv.speech_s:.0f} s of speech: read the text aloud for about 30 seconds")
        v = voiced_audio(x, lv)[: int(ENROLL_MAX_S * RATE)]
        w, h = int(ENROLL_WINDOW_S * RATE), int(ENROLL_HOP_S * RATE)
        embs = [e for e in (self.model.embed(v[i:i + w]) for i in range(0, max(1, len(v) - w + 1), h)) if e is not None]
        if len(embs) < 3:
            raise SpeakerError("not enough clear speech: read the text aloud for about 30 seconds")
        E = np.stack(embs)
        sims = E @ E.T
        np.fill_diagonal(sims, np.nan)
        agree = np.nanmedian(sims, axis=1)
        keep = agree >= ENROLL_AGREE
        if keep.mean() < ENROLL_MIN_KEPT:
            raise SpeakerError("that sounded like more than one voice, or a lot of noise: try again somewhere quiet, alone")
        m = E[keep].mean(axis=0)
        m = m / float(np.linalg.norm(m))
        info = {"at": self.now().isoformat(timespec="seconds"), "speech_s": round(lv.speech_s, 1), "windows": len(embs),
                "kept": int(keep.sum()), "agreement": round(float(np.median(agree[keep])), 2)}
        secretbox.save(self.store, {"v": 1, "model": MODEL_NAME, "dim": int(m.size),
                                    "vector": [round(float(a), 6) for a in m], **info})
        self._print = None
        return info

    # the setting
    def mode(self) -> str:
        """Read at most every 5 s (every turn asks; the call's event loop shouldn't wait for the database)."""
        from sqlmodel import Session

        from .db import SettingKV
        if self.engine is None:
            return DEFAULT_MODE
        if self._mode is not None and time.monotonic() - self._mode[0] < 5:
            return self._mode[1]
        with Session(self.engine) as s:
            row = s.get(SettingKV, MODE_KEY)
        mode = row.value if row is not None and row.value in MODES else DEFAULT_MODE
        self._mode = (time.monotonic(), mode)
        return mode

    def set_mode(self, mode: str) -> str:
        from sqlmodel import Session

        from .db import SettingKV
        if mode not in MODES:
            raise SpeakerError(f"the speaker check is one of {', '.join(MODES)}")
        with Session(self.engine) as s:
            row = s.get(SettingKV, MODE_KEY) or SettingKV(key=MODE_KEY)
            row.value = mode
            s.add(row)
            s.commit()
        self._mode = (time.monotonic(), mode)
        return mode

    def check_state(self) -> tuple[bool, str | None]:
        """(the speaker check runs, why not)."""
        if np is None:
            return False, "the voice packages are not installed"
        if self.mode() == "off":
            return False, "set to off (Settings → Voice)"
        if not self.model.installed():
            return False, "model missing (run Update Kairo)"
        if self.model.failed:
            return False, f"the model failed: {self.model.failed}"
        info = self.enrolled()
        if not info:
            return False, "until you teach Kairo your voice (Settings → Voice)"
        if info.get("problem"):
            return False, info["problem"]
        return True, None

    def label_gate(self) -> dict:
        """G13, for kairo diag: is the label gate (g13.speaker_label_gate) on, and does the check run? With no check
        every voice request is unchecked and can change things, as before: that is the diag's WARN."""
        from sqlmodel import Session

        from . import flags
        flag = flags.FLAGS["g13.speaker_label_gate"][0]
        if self.engine is not None:
            try:
                with Session(self.engine) as s:
                    flag = flags.on(s, "g13.speaker_label_gate")
            except Exception:   # the report never fails over it
                log.warning("speaker check: the label gate's switch could not be read", exc_info=True)
        on, why = self.check_state()
        return {"flag": flag, "checked": on, "note": why}

    def device(self) -> dict:
        """Voice speed's row for the speaker check (like Whisper's and Kokoro's)."""
        on, why = self.check_state()
        out = {"device": self.model.device, "model": MODEL_NAME.removesuffix(".onnx"), "loaded": self.model.loaded,
               "load_ms": self.model.load_ms, "warm_ms": self.model.warm_ms, "on": on, "note": why,
               "checked": getattr(self.model, "checked", None), "label_gate": self.label_gate()}
        if not self.model.installed():
            out.update(ok=False, reason="model missing (run Update Kairo): the speaker check is off, calls still work")
        elif self.model.failed:
            out.update(ok=False, reason=f"the model failed: {self.model.failed}")
        elif not self.model.loaded:
            out.update(ok=not on, reason=why or "not loaded yet (it loads when Kairo starts)")
        else:
            out.update(ok=self.model.device == "cuda",
                       reason=None if self.model.device == "cuda" else f"on the CPU: {self.model.why or 'no GPU'}")
        return out

    def warm(self) -> None:
        if self.check_state()[0]:
            self.model.warm()

    # one turn
    def measure(self, pcm: bytes, with_print: bool) -> "Measured":
        if np is None:                                   # no voice packages: nothing to measure, answered
            return Measured(Level(0.0, None, None), None, 0)
        t0 = time.monotonic()
        x = pcm16(pcm)
        lv = level(x)
        emb, failed = None, None
        if with_print and lv.speech_s >= MIN_CHECK_S:
            try:
                emb = self.model.embed(voiced_audio(x, lv))
            except Exception as exc:   # never costs the turn: it is answered
                failed = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("speaker check failed on a turn (answered): %s", failed)
        return Measured(lv, emb, int((time.monotonic() - t0) * 1000), failed)

    def gate(self, path: str = "call", on_ignored: Callable[["Verdict"], object] | None = None) -> "CallGate":
        return CallGate(self, path=path, on_ignored=on_ignored)

    # the record (no words, ever)
    def record(self, v: "Verdict") -> None:
        from sqlmodel import Session

        from .db import SettingKV
        if self.engine is None:
            return
        row_ = [v.at, v.path, v.outcome, v.why, v.score, v.rms_db, v.owner_db, v.speech_s, v.ms, v.wait_ms,
                self.model.device if v.score is not None else None]
        try:
            with Session(self.engine) as s:
                row = s.get(SettingKV, STATS_KEY) or SettingKV(key=STATS_KEY)
                row.value = json.dumps(_rows(row.value)[-(KEEP_TURNS - 1):] + [row_])
                s.add(row)
                s.commit()
        except Exception:   # Voice speed misses one turn; the call goes on
            log.warning("speaker check: the turn's record was not written", exc_info=True)

    def summary(self, s, days: int = 7) -> dict:
        from .db import SettingKV
        row = s.get(SettingKV, STATS_KEY)
        return summarize(_rows(row.value if row else None), self.now(), days)

    def state(self, s) -> dict:
        on, why = self.check_state()
        return {"mode": self.mode(), "modes": list(MODES), "enrolled": self.enrolled(), "check": on, "why_off": why,
                "model": self.device(), "record_s": 30, "min_speech_s": ENROLL_MIN_S, **self.summary(s)}


COLUMNS = ("at", "path", "outcome", "why", "score", "rms_db", "owner_db", "speech_s", "ms", "wait_ms", "device")


def _rows(value) -> list:
    try:
        data = json.loads(value) if value else []
    except ValueError:
        data = []
    return [r for r in data if isinstance(r, list) and len(r) == len(COLUMNS)] if isinstance(data, list) else []


def _pct(v: list, p: float):
    v = sorted(x for x in v if isinstance(x, (int, float)))
    return v[min(len(v) - 1, int(len(v) * p))] if v else None


def summarize(rows: list, now: dt.datetime, days: int = 7) -> dict:
    """Counts and times for Voice speed and Run diagnosis; the ignored turns with time, reason and score."""
    since = (now - dt.timedelta(days=days)).isoformat(timespec="seconds")
    recent = [dict(zip(COLUMNS, r)) for r in rows if str(r[0]) >= since]
    counts = {"answered": 0, "not_you": 0, "too_quiet": 0}
    for r in recent:
        if r["outcome"] in counts:
            counts[r["outcome"]] += 1
    scored = [r for r in recent if r["score"] is not None]
    ignored = [{"at": r["at"], "reason": r["outcome"], "title": REASONS[r["outcome"]], "score": r["score"],
                "level_db": r["rms_db"], "owner_db": r["owner_db"], "speech_s": r["speech_s"]}
               for r in reversed(recent) if r["outcome"] in REASONS][:20]
    return {"days": days, "turns": len(recent), "counts": counts, "scored": len(scored),
            "ms": {"p50": _pct([r["ms"] for r in scored], .5), "p95": _pct([r["ms"] for r in scored], .95),
                   "last": scored[-1]["ms"] if scored else None},
            "wait_ms": {"p50": _pct([r["wait_ms"] for r in recent], .5), "p95": _pct([r["wait_ms"] for r in recent], .95)},
            "device": scored[-1]["device"] if scored else None, "ignored": ignored}


# ---------------------------------------------------------------- one call: the turn's audio in, a verdict out

@dataclass
class Measured:
    level: Level
    emb: object                         # np.ndarray | None
    ms: int
    failed: str | None = None


@dataclass
class Verdict:
    answer: bool
    outcome: str                        # "answered" | "not_you" | "too_quiet"
    why: str                            # e.g. "you", "short: you", "off until ...", "slow"
    score: float | None = None
    rms_db: float | None = None
    peak_db: float | None = None
    owner_db: float | None = None       # the owner's level on this call (median of the answered turns)
    speech_s: float = 0.0
    ms: int = 0                         # the check's own time (level + voiceprint), all pieces of the turn
    wait_ms: int = 0                    # what the turn waited for it (the latency it added)
    path: str = "call"
    at: str = ""
    label: str = UNCHECKED              # G13: owner | unsure | stranger | unchecked (kept off the record and the log)

    def log_line(self) -> str:
        """Score and reason only, never the words."""
        return (f"speaker check: {REASONS.get(self.outcome, self.outcome)} (score {self.score}, level {self.rms_db} dB, "
                f"owner {self.owner_db} dB, {self.speech_s} s of speech, {self.ms} ms)")


@dataclass
class _Piece:
    future: concurrent.futures.Future
    words: bool | None = None           # None: still being written down; False: no words (not part of a turn)
    at: float = field(default_factory=time.monotonic)


class CallGate:
    """One call. hear(pcm) for each piece of speech (in parallel with Whisper), words(piece, ...) once Whisper
    has written it down, decide() when a turn is finished: answer it or ignore it."""

    def __init__(self, speaker: Speaker, *, path: str = "call", on_ignored=None):
        self.speaker, self.path, self.on_ignored = speaker, path, on_ignored
        self.pieces: list[_Piece] = []
        self.owner: list[tuple[float, float]] = []    # (rms, peak) of this call's answered turns
        self.quiet_run = 0
        self._lock = threading.Lock()
        self._answered: list[str] = []                # G13: labels of turns answer() let through, not yet taken by their answer

    def hear(self, pcm: bytes) -> _Piece:
        """Thread-safe (the speech-to-text thread calls it). The check starts at once, in its own thread."""
        piece = _Piece(self.speaker.pool.submit(self.speaker.measure, bytes(pcm), self.speaker.check_state()[0]))
        with self._lock:
            self.pieces.append(piece)
        return piece

    @staticmethod
    def words(piece: _Piece, words: bool) -> None:
        piece.words = bool(words)

    async def decide(self) -> Verdict:
        """The finished turn: the pieces Whisper found words in since the last turn. Never raises."""
        t0 = time.monotonic()
        with self._lock:
            ready = [p for p in self.pieces if p.words is not None]
            self.pieces = [p for p in self.pieces if p.words is None]
        use = [p for p in ready if p.words]
        try:
            measured = await asyncio.wait_for(asyncio.gather(*(asyncio.wrap_future(p.future) for p in use)), WAIT_MAX_S)
            v = self.judge(list(measured))
        except asyncio.TimeoutError:
            v = Verdict(True, "answered", "slow", label=self._unsure_if_on())
            log.warning("speaker check: not ready after %d ms, the turn is answered", int(WAIT_MAX_S * 1000))
        except Exception as exc:   # never costs the turn
            log.warning("speaker check: %s: %s (the turn is answered)", type(exc).__name__, exc)
            v = Verdict(True, "answered", "error", label=self._unsure_if_on())
        v.wait_ms = int((time.monotonic() - t0) * 1000)
        return self._done(v)

    def judge(self, measured: list[Measured]) -> Verdict:
        on, why_off = self.speaker.check_state()
        mode = self.speaker.mode() if on else "off"
        frames = sum(m.level.frames for m in measured)
        speech = round(sum(m.level.speech_s for m in measured), 2)
        rms = _db(sum(m.level.power * m.level.frames for m in measured) / frames) if frames else None
        peaks = [m.level.peak_db for m in measured if m.level.peak_db is not None]
        peak = max(peaks) if peaks else None
        owner_db = (statistics.median(r for r, _ in self.owner[-QUIET_REF_TURNS:])
                    if len(self.owner) >= QUIET_MIN_REF else None)
        score = None
        embs = [(m.emb, m.level.speech_s) for m in measured if m.emb is not None]
        vp = self.speaker.voiceprint() if on else None
        if embs and vp is not None:
            score = round(cosine(sum(e * w for e, w in embs), vp), 3)
        if score is not None:
            ok, why = speaker_verdict(score, speech, mode)
        elif on:
            ok, why = True, ("too short to tell" if speech < MIN_CHECK_S
                             else next((m.failed for m in measured if m.failed), None) or "no score")
        else:
            ok, why = True, why_off or "off"
        base = dict(score=score, rms_db=rms, peak_db=peak, owner_db=owner_db, speech_s=speech,
                    ms=sum(m.ms for m in measured), path=self.path)
        label = label_for(on=on, ok=ok, score=score, mode=mode)
        if not ok:
            return Verdict(False, "not_you", why, label=label, **base)
        surely_you = score is not None and score >= THRESHOLD.get(mode, 2.0)
        if too_quiet(rms, peak, self.owner) and not surely_you:   # far away, and not known to be the owner
            self.quiet_run += 1
            if self.quiet_run >= QUIET_RESET_AFTER:
                self.owner, self.quiet_run = [], 0           # the owner moved away: learn the level again
            return Verdict(False, "too_quiet", "too quiet / far", label=UNSURE, **base)   # ignored: nobody ran, but not a confirmed owner
        self.quiet_run = 0
        if rms is not None and peak is not None:
            self.owner = (self.owner + [(rms, peak)])[-QUIET_REF_TURNS:]
        return Verdict(True, "answered", why, label=label, **base)

    def _unsure_if_on(self) -> str:
        """The label of a turn the check had no verdict for (too slow, crashed): unsure when the check is on, since then
        Kairo could not say it was the owner; unchecked when it is off. A check that cannot even tell is unsure."""
        try:
            return UNSURE if self.speaker.check_state()[0] else UNCHECKED
        except Exception:
            return UNSURE

    def _done(self, v: Verdict) -> Verdict:
        v.path = self.path
        v.at = self.speaker.now().isoformat(timespec="seconds")
        if not v.answer:
            log.info(v.log_line())
            if self.on_ignored is not None:
                try:
                    self.on_ignored(v)
                except Exception:   # the phone is gone: the call goes on
                    log.debug("speaker check: on_ignored failed", exc_info=True)
        try:
            self.speaker.writer.submit(self.speaker.record, v)   # off the call's event loop
        except RuntimeError:   # shutting down
            pass
        return v

    async def answer(self) -> bool:
        """voicertc's hook: True = answer this turn. An answered turn's label waits for its answer (take_label)."""
        v = await self.decide()
        if v.answer:
            self._queue_label(v.label)
        return v.answer

    def _queue_label(self, label: str) -> None:
        with self._lock:
            self._answered.append(label)
            if len(self._answered) > MAX_WAITING_LABELS:     # never grows when answers are slow: the two oldest become their worst
                self._answered[:2] = [worst(*self._answered[:2])]

    def take_label(self) -> str:
        """G13: the label for the turn that is about to be answered (the call's answer callback, first thing it does):
        the least trusted of the verdicts answer() let through since the last take, so no order of events makes a turn
        more trusted than its own verdict. Nothing waiting (a bug: an answer without a verdict) is unsure, never owner."""
        with self._lock:
            label, self._answered = worst(*self._answered), []
        return label or UNSURE


class Listening:
    """The voice as voicertc sees it, with one addition: each piece of speech also goes to the call's
    gate, in parallel with Whisper, so the check is ready when the turn is."""

    def __init__(self, voice, gate: CallGate):
        self._voice, self._gate = voice, gate

    def __getattr__(self, name):
        return getattr(self._voice, name)

    def transcribe_pcm(self, pcm: bytes, language=None):
        from .voicelink import heard
        piece = self._gate.hear(pcm)
        words = False
        try:
            tr = self._voice.transcribe_pcm(pcm, language)
            words = bool(heard(getattr(tr, "text", "") or ""))
            return tr
        finally:
            CallGate.words(piece, words)
