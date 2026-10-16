"""Smart endpointing (owner's request 2026-10-16): is the owner done, or only pausing?

Pipecat's Smart Turn v3 (open source, BSD-2, about 8M parameters, ONNX on the CPU in tens of ms; 23
languages including English and German) listens to the last 8 seconds of audio (how it was said, not
the words) and gives the probability that the turn is complete. Kairo asks it at the first short
pause (about 200 ms): done -> Kairo answers at once; not done -> it waits for more (or a longer quiet).
Without the model (not installed yet) the plain quiet rule stays.

The features are Whisper's (80 log-mel bins, 25 ms windows, 10 ms hop), made here with numpy so the
voice packages need nothing new. Which side short audio is padded on is a setting the voice lab can
check on this PC (pad_side).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

try:
    import numpy as np                                  # comes with the voice packages
except ImportError:                                     # the app runs without them: no turn detector
    np = None

log = logging.getLogger("kairo.smartturn")

RATE = 16000
SECONDS = 8
SAMPLES = RATE * SECONDS
N_FFT = 400
HOP = 160
N_MELS = 80
COMPLETE_AT = 0.5


def _hz_to_mel(f):
    """Slaney mel scale (librosa's default, what Whisper uses)."""
    f = np.asarray(f, dtype=np.float64)
    min_log_hz, min_log_mel, logstep = 1000.0, 15.0, np.log(6.4) / 27.0
    mel = 3.0 * f / 200.0
    return np.where(f >= min_log_hz, min_log_mel + np.log(np.maximum(f, 1e-10) / min_log_hz) / logstep, mel)


def _mel_to_hz(m):
    m = np.asarray(m, dtype=np.float64)
    min_log_hz, min_log_mel, logstep = 1000.0, 15.0, np.log(6.4) / 27.0
    return np.where(m >= min_log_mel, min_log_hz * np.exp(logstep * (m - min_log_mel)), 200.0 * m / 3.0)


def mel_filters(n_mels: int = N_MELS, n_fft: int = N_FFT, rate: int = RATE) -> np.ndarray:
    """(n_mels, n_fft // 2 + 1) triangular filters, slaney-normalised (= librosa.filters.mel)."""
    fft_freqs = np.linspace(0, rate / 2, n_fft // 2 + 1)
    mel_pts = _mel_to_hz(np.linspace(_hz_to_mel(0.0), _hz_to_mel(rate / 2), n_mels + 2))
    fdiff = np.diff(mel_pts)
    ramps = mel_pts[:, None] - fft_freqs[None, :]
    lower = -ramps[:-2] / fdiff[:-1, None]
    upper = ramps[2:] / fdiff[1:, None]
    weights = np.maximum(0, np.minimum(lower, upper))
    weights *= (2.0 / (mel_pts[2:n_mels + 2] - mel_pts[:n_mels]))[:, None]
    return weights.astype(np.float32)


_CONST: dict = {}


def _const():
    if "window" not in _CONST:                              # set last: once it's there, both are
        _CONST["filters"] = mel_filters()
        _CONST["window"] = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N_FFT) / N_FFT)).astype(np.float32)   # periodic Hann
    return _CONST["filters"], _CONST["window"]


def features(samples: np.ndarray, pad_side: str = "right") -> np.ndarray:
    """(1, 80, 800) float32 from up to the last 8 s of 16 kHz float audio: normalised, padded, log-mel."""
    x = np.asarray(samples, dtype=np.float32)[-SAMPLES:]
    n = len(x)
    if n:
        x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)
    pad = SAMPLES - n
    x = np.pad(x, (pad, 0) if pad_side == "left" else (0, pad))
    padded = np.pad(x, (N_FFT // 2, N_FFT // 2), mode="reflect")
    frames = 1 + (len(padded) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(frames)[:, None]
    filters, window = _const()
    power = np.abs(np.fft.rfft(padded[idx] * window, axis=1)) ** 2
    mel = filters @ power[:-1].T                          # Whisper drops the last frame: 800 frames
    log_spec = np.log10(np.maximum(mel, 1e-10))
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    return ((log_spec + 4.0) / 4.0).astype(np.float32)[None]


def pcm16(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0


class SmartTurn:
    """probability(pcm) -> 0..1 that the owner finished their turn."""

    def __init__(self, model_path=None, *, session=None, pad_side: str = "right", threads: int = 1):
        """model_path: the model file, or a function that finds it (it can arrive after Kairo started)."""
        self._path, self.pad_side, self.threads = model_path, pad_side, threads
        self._session = session
        self._lock = threading.Lock()
        self.failed: str | None = None
        self.last_ms: int | None = None

    @property
    def path(self) -> Path | None:
        return self._path() if callable(self._path) else self._path

    def _load(self):
        if self._session is None:
            import onnxruntime as ort                       # comes with the voice packages (kokoro-onnx)
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = self.threads
            opts.inter_op_num_threads = 1
            self._session = ort.InferenceSession(str(self.path), sess_options=opts, providers=["CPUExecutionProvider"])
        return self._session

    def available(self) -> bool:
        return np is not None and self.failed is None and (self._session is not None or (self.path is not None and Path(self.path).is_file()))

    def probability(self, pcm: bytes) -> float | None:
        """None: no model, or it failed (the plain quiet rule then decides)."""
        if not self.available():
            return None
        t = time.monotonic()
        try:
            with self._lock:
                sess = self._load()
                name = sess.get_inputs()[0].name
                out = sess.run(None, {name: features(pcm16(pcm), self.pad_side)})
            p = float(np.asarray(out[0]).reshape(-1)[0])
        except Exception as exc:   # never cost a turn: fall back to the quiet rule
            self.failed = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("smart turn failed, using the quiet rule: %s", self.failed)
            return None
        if not 0.0 <= p <= 1.0:                             # a logit, not a probability
            p = float(1 / (1 + np.exp(-p)))
        self.last_ms = int((time.monotonic() - t) * 1000)
        return p

    def warm(self) -> None:
        self.probability(b"\x00\x00" * RATE)
