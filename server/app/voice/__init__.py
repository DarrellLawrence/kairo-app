"""Local voice (phase 3): speech to text with faster-whisper, speech with
Kokoro (English) and Piper (German). Everything runs on kairo-home; no audio
leaves the PC. The libraries are optional (requirements-voice.txt): without
them the app still runs and voice reports "not installed".

Models live in <data>/models and are downloaded once by the installer
(python -m app.voice download). The PC's RTX 3060 runs Whisper; if CUDA can't
be loaded, Whisper runs on the CPU with a smaller model.
"""

from __future__ import annotations

import io
import logging
import os
import re
import sys
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("kairo.voice")

WHISPER_GPU = os.environ.get("KAIRO_WHISPER_MODEL", "large-v3-turbo")
WHISPER_CPU = os.environ.get("KAIRO_WHISPER_CPU_MODEL", "small")
KOKORO_FILES = {
    "kokoro-v1.0.int8.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}
KOKORO_VOICE = os.environ.get("KAIRO_KOKORO_VOICE", "af_heart")
PIPER_DE = os.environ.get("KAIRO_PIPER_VOICE", "de_DE-thorsten-medium")
RETRY_LOAD_AFTER = 600   # seconds before a failed model load is tried again
GPU_RUNTIME = re.compile(r"cuda|cublas|cudnn|\.dll|library", re.I)
MAX_AUDIO = 15 * 1024 * 1024
MAX_SPEAK = 3000

_DE_WORDS = set("und der die das ist nicht ich du wir ihr sie es ein eine mit auf für dass heute morgen bitte danke "
                "noch schon auch aber oder wenn dann hast habe bin sind wird werden kann".split())


class VoiceError(Exception):
    pass


class NotInstalled(VoiceError):
    pass


def language_of(text: str) -> str:
    """'de' or 'en' for picking the voice. Simple and good enough for two languages."""
    words = re.findall(r"[a-zäöüß]+", text.lower())
    if not words:
        return "en"
    de = sum(1 for w in words if w in _DE_WORDS) + 2 * len(re.findall(r"[äöüß]", text.lower()))
    return "de" if de / max(1, len(words)) > 0.12 else "en"


def to_wav(samples, rate: int) -> bytes:
    import numpy as np
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def use_site(site_dir: Path | None) -> None:
    """The installer puts the voice packages in C:\\Kairo\\tools\\voice (shared by all releases)."""
    if site_dir and Path(site_dir).is_dir() and str(site_dir) not in sys.path:
        sys.path.append(str(site_dir))


def _add_cuda_dlls() -> None:
    """Windows: CTranslate2 needs cuBLAS/cuDNN DLLs from the nvidia-* wheels on the DLL path."""
    if sys.platform != "win32":
        return
    import site
    for base in list(site.getsitepackages()) + [p for p in sys.path if p.endswith("voice")]:
        root = Path(base) / "nvidia"
        for bin_dir in root.glob("*/bin"):
            try:
                os.add_dll_directory(str(bin_dir))
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            except OSError:
                pass


@dataclass
class Transcript:
    text: str
    language: str
    seconds: float
    took: float


class Voice:
    def __init__(self, models_dir: Path, site_dir: Path | None = None):
        self.models_dir = Path(models_dir)
        use_site(Path(os.environ["KAIRO_VOICE_SITE"]) if os.environ.get("KAIRO_VOICE_SITE") else site_dir)
        self._stt = None
        self._stt_device = None
        self._stt_failed = 0.0
        self._kokoro = None
        self._piper = None
        self._lock = threading.Lock()   # one model load at a time
        self.last_error: str | None = None

    # ---------------------------------------------------------------- status

    @staticmethod
    def installed() -> dict[str, bool]:
        import importlib.util as u
        return {"stt": u.find_spec("faster_whisper") is not None, "en": u.find_spec("kokoro_onnx") is not None,
                "de": u.find_spec("piper") is not None}

    def status(self) -> dict:
        inst = self.installed()
        return {"installed": inst, "stt_device": self._stt_device, "stt_loaded": self._stt is not None,
                "en_ready": inst["en"] and all((self.models_dir / "kokoro" / f).is_file() for f in KOKORO_FILES),
                "de_ready": inst["de"] and (self.models_dir / "piper" / f"{PIPER_DE}.onnx").is_file(),
                "last_error": self.last_error}

    # ---------------------------------------------------------------- speech to text

    def _local_model(self, name: str) -> str:
        """The downloaded model folder, so loading needs no Hugging Face lookup (network, lock files);
        the plain name only when Update Kairo hasn't fetched it yet."""
        found = sorted((self.models_dir / "whisper").glob(f"models--*--faster-whisper-{name}/snapshots/*/model.bin"))
        return str(found[-1].parent) if found else name

    def _make_stt(self, device: str):
        from faster_whisper import WhisperModel
        root = str(self.models_dir / "whisper")
        if device == "cuda":
            _add_cuda_dlls()
            return WhisperModel(self._local_model(WHISPER_GPU), device="cuda", compute_type="float16", download_root=root)
        return WhisperModel(self._local_model(WHISPER_CPU), device="cpu", compute_type="int8", download_root=root)

    def _load_stt(self):
        if self._stt is not None:
            return self._stt
        if not self.installed()["stt"]:
            raise NotInstalled("Speech recognition is not installed on the PC (run Update Kairo).")
        if self._stt_failed and time.monotonic() - self._stt_failed < RETRY_LOAD_AFTER:
            raise NotInstalled(f"Speech recognition could not start on the PC: {self.last_error}")
        try:
            self._stt, self._stt_device = self._make_stt("cuda"), "cuda"
        except Exception as exc:   # no GPU / CUDA DLLs missing: CPU, smaller model
            log.warning("Whisper on GPU failed (%s); using the CPU", exc)
            try:
                self._stt, self._stt_device = self._make_stt("cpu"), "cpu"
            except Exception as exc2:   # never a bare crash: say why, and don't retry slowly on every sentence
                self._stt_failed = time.monotonic()
                self.last_error = f"load: {type(exc2).__name__}: {exc2}"
                log.error("Whisper could not load: %s", self.last_error)
                raise NotInstalled(f"Speech recognition could not start on the PC: {self.last_error}") from exc2
        self._stt_failed = 0.0
        return self._stt

    def warm(self) -> None:
        """Load the models ahead of the first word (called at start, in the background)."""
        for load in (self._load_stt, self._load_kokoro):
            try:
                with self._lock:
                    load()
            except Exception as exc:   # reported by status(); the request itself will say why
                log.info("voice warm-up: %s", exc)

    def transcribe(self, audio: bytes, language: str | None = None) -> Transcript:
        if not audio:
            raise VoiceError("no audio")
        if len(audio) > MAX_AUDIO:
            raise VoiceError("recording too long")
        with self._lock:
            model = self._load_stt()
        t = time.monotonic()
        try:
            segments, info = model.transcribe(io.BytesIO(audio), language=language, vad_filter=True, beam_size=5)
            text = " ".join(s.text.strip() for s in segments).strip()
        except Exception as exc:
            if self._stt_device == "cuda" and GPU_RUNTIME.search(str(exc)):
                # CUDA loaded the model but can't run it (cuBLAS/cuDNN DLL missing): CPU from now on
                log.warning("Whisper on GPU failed at run time (%s); switching to the CPU", exc)
                with self._lock:
                    try:
                        self._stt, self._stt_device = self._make_stt("cpu"), "cpu"
                    except Exception as exc2:
                        self._stt, self.last_error = None, f"load: {type(exc2).__name__}: {exc2}"
                        raise NotInstalled(f"Speech recognition could not start on the PC: {self.last_error}") from exc2
                return self.transcribe(audio, language)
            self.last_error = f"transcribe: {exc}"
            raise VoiceError(f"could not understand the audio ({type(exc).__name__})") from exc
        return Transcript(text=text, language=info.language, seconds=round(info.duration, 1), took=round(time.monotonic() - t, 2))

    # ---------------------------------------------------------------- text to speech

    def _load_kokoro(self):
        if self._kokoro is None:
            if not self.installed()["en"]:
                raise NotInstalled("The English voice is not installed on the PC.")
            d = self.models_dir / "kokoro"
            if not all((d / f).is_file() for f in KOKORO_FILES):
                raise NotInstalled("The English voice model is missing (run Update Kairo).")
            from kokoro_onnx import Kokoro
            self._kokoro = Kokoro(str(d / "kokoro-v1.0.int8.onnx"), str(d / "voices-v1.0.bin"))
        return self._kokoro

    def _load_piper(self):
        if self._piper is None:
            if not self.installed()["de"]:
                raise NotInstalled("The German voice is not installed on the PC.")
            model = self.models_dir / "piper" / f"{PIPER_DE}.onnx"
            if not model.is_file():
                raise NotInstalled("The German voice model is missing (run Update Kairo).")
            from piper import PiperVoice
            self._piper = PiperVoice.load(model)
        return self._piper

    def speak(self, text: str, language: str | None = None) -> tuple[bytes, str]:
        """WAV bytes and the language used. Long texts are cut at a sentence end."""
        text = _clean_for_speech(text)
        if not text:
            raise VoiceError("nothing to say")
        lang = language or language_of(text)
        with self._lock:
            if lang == "de":
                try:
                    voice = self._load_piper()
                except NotInstalled:
                    lang = "en"   # better an English voice than none
            if lang != "de":
                kokoro = self._load_kokoro()
        if lang == "de":
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                voice.synthesize_wav(text, w)
            return buf.getvalue(), "de"
        samples, rate = kokoro.create(text, voice=KOKORO_VOICE, lang="en-us")
        return to_wav(samples, rate), "en"

    # ---------------------------------------------------------------- downloads (installer)

    def download(self) -> list[str]:
        """Fetch every model once, into <data>/models. Returns what was done."""
        import urllib.request
        done = []
        kd = self.models_dir / "kokoro"
        kd.mkdir(parents=True, exist_ok=True)
        for name, url in KOKORO_FILES.items():
            target = kd / name
            if not target.is_file():
                tmp = target.with_suffix(".part")
                urllib.request.urlretrieve(url, tmp)   # noqa: S310 (fixed https URL)
                tmp.replace(target)
                done.append(name)
        if self.installed()["de"]:
            pd = self.models_dir / "piper"
            pd.mkdir(parents=True, exist_ok=True)
            if not (pd / f"{PIPER_DE}.onnx").is_file():
                from piper.download_voices import download_voice
                download_voice(PIPER_DE, pd)
                done.append(PIPER_DE)
        if self.installed()["stt"]:
            from faster_whisper import download_model   # same cache WhisperModel(download_root=...) uses
            for name in (WHISPER_CPU, WHISPER_GPU):
                download_model(name, cache_dir=str(self.models_dir / "whisper"))
                done.append(f"whisper {name}")
        return done


def _clean_for_speech(text: str) -> str:
    t = re.sub(r"```.*?```", " ", text or "", flags=re.S)          # no code blocks
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)                    # links -> their text
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[*_#>`|]+", " ", t)                                  # markdown marks
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > MAX_SPEAK:
        cut = t.rfind(".", 0, MAX_SPEAK)
        t = t[: cut + 1 if cut > 200 else MAX_SPEAK]
    return t
