"""Local voice (phase 3): speech to text with faster-whisper, speech with
Kokoro (English) and Piper (German). Everything runs on kairo-home; no audio
leaves the PC. The libraries are optional (requirements-voice.txt): without
them the app still runs and voice reports "not installed".

Models live in tools\\voice-<hash>\\models and are downloaded once by the installer
(python -m app.voice download). The PC's RTX 3060 runs Whisper (CTranslate2) and Kokoro (ONNX
Runtime, CUDA); if CUDA can't be loaded they run on the CPU, and devices() says so and why
(owner's request 2026-10-17: never a silent CPU fallback).
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

from . import langid

log = logging.getLogger("kairo.voice")

WHISPER_GPU = os.environ.get("KAIRO_WHISPER_MODEL", "large-v3-turbo")
WHISPER_CPU = os.environ.get("KAIRO_WHISPER_CPU_MODEL", "small")
# fp16, not int8 (2026-10-16): the int8 model gave NaN (= silence) or audio far too loud (= noise) on
# ordinary sentences ("Sure, I will remind you.") and was 5x slower on the CPU. Named in
# requirements-voice.txt too: a new model = a new tools folder, downloaded clean.
KOKORO_MODEL = "kokoro-v1.0.fp16.onnx"
KOKORO_FILES = {
    KOKORO_MODEL: f"https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/{KOKORO_MODEL}",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}
KOKORO_VOICE = os.environ.get("KAIRO_KOKORO_VOICE", "af_heart")
PIPER_DE = os.environ.get("KAIRO_PIPER_VOICE", "de_DE-thorsten-medium")
GERMAN_TEST = "Guten Morgen, das ist ein Test."      # what the self-test has the German voice say, for Whisper to write down
GERMAN_TEST_WORDS = ("guten", "morgen", "test")        # any one of them counts (a small model may hear the rest wrong)
RETRY_LOAD_AFTER = 600   # seconds before a failed model load is tried again
WARM_FRESH_S = 15        # a call connecting this soon after a decode finds Whisper warm: no warm-up
# What Whisper warms up on: Kairo's own voices say these (made-up sentences, about 4 s each), so the first
# real sentence of a call meets kernels that already ran with speech of that length.
WARM_TEXT = {"en": "Remind me to call the dentist tomorrow at three in the afternoon.",
             "de": "Erinnere mich morgen um drei Uhr nachmittags an den Zahnarzt."}
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


def _samples(pcm: bytes):
    import numpy as np
    return np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0


def _wav_16k(wav: bytes) -> bytes:
    """A WAV's samples as 16 kHz 16-bit mono PCM (what the phone sends), by linear interpolation."""
    import numpy as np
    with wave.open(io.BytesIO(wav), "rb") as w:
        rate, channels = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
    x = x.reshape(-1, channels).mean(axis=1) if channels > 1 else x
    if rate != 16000:
        x = np.interp(np.linspace(0, len(x) - 1, int(len(x) * 16000 / rate)), np.arange(len(x)), x)
    return x.astype("<i2").tobytes()


def _package_version(name: str) -> str | None:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return None


def _ct2_version() -> str | None:
    """CTranslate2's version for devices(): from the library when Whisper has loaded it, else from its metadata
    (importing it for a status page would load its DLLs)."""
    ct2 = sys.modules.get("ctranslate2")
    return getattr(ct2, "__version__", None) or _package_version("ctranslate2")


def sound(samples):
    """Never silence or noise from a model that went numerically wrong: no samples or not-a-number is an
    error (said, not sent); audio over full scale is brought back to it, not clipped into noise."""
    import numpy as np
    a = np.asarray(samples, dtype=np.float32)
    if a.size == 0 or not np.isfinite(a).all():
        raise VoiceError("the voice model made no usable audio for this sentence")
    peak = float(np.abs(a).max())
    return a / peak * 0.95 if peak > 1.0 else a


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


def wav_problems(wav: bytes) -> list[str]:
    """What's wrong with a TTS answer before it goes to the phone: [] when it's a valid, non-silent
    16-bit PCM WAV of at least 0.3 s that iOS Safari can play (fix A1)."""
    import array
    try:
        with wave.open(io.BytesIO(wav), "rb") as w:
            channels, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            pcm = w.readframes(frames)
    except (wave.Error, EOFError) as exc:
        return [f"not a WAV: {exc}"]
    problems = []
    if width != 2:
        problems.append(f"{width * 8}-bit samples (iOS wants 16-bit PCM)")
    if channels not in (1, 2):
        problems.append(f"{channels} channels")
    if not 8000 <= rate <= 48000:
        problems.append(f"sample rate {rate} Hz")
    if rate and frames / rate < 0.3:
        problems.append(f"only {frames / rate:.2f} s long")
    if width == 2 and pcm:
        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) // 2 * 2])
        if sys.byteorder == "big":
            samples.byteswap()
        rms = (sum(x * x for x in samples) / len(samples)) ** 0.5 / 32768
        if rms < 0.005:
            problems.append(f"silent (level {rms:.4f})")
    return problems


def use_site(site_dir: Path | None) -> None:
    """The installer puts the voice packages in C:\\Kairo\\tools\\voice-<hash> (one per package list)."""
    if site_dir and Path(site_dir).is_dir() and str(site_dir) not in sys.path:
        sys.path.append(str(site_dir))


def cuda_dll_dirs(site_dir: Path | None) -> list[Path]:
    """Where the nvidia-* wheels put their DLLs: the voice packages' own folder first (since 2026-10-14 it
    is tools\\voice-<hash>, so a name test like endswith("voice") never found it: Whisper fell back to the
    CPU on every start, 2026-10-17), then the Python's own site-packages."""
    import site
    bases = ([Path(site_dir)] if site_dir else []) + [Path(b) for b in site.getsitepackages()]
    return [d for base in bases for d in sorted((base / "nvidia").glob("*/bin")) if d.is_dir()]


_dlls_added: set[str] = set()


def _add_cuda_dlls(site_dir: Path | None = None) -> list[str]:
    """Windows: CTranslate2 (Whisper) and ONNX Runtime (Kokoro) need cuBLAS/cuDNN/CUDA runtime DLLs from
    the nvidia-* wheels on the DLL path. Returns the folders added."""
    if sys.platform != "win32":
        return []
    added = []
    for bin_dir in cuda_dll_dirs(site_dir):
        if str(bin_dir) in _dlls_added:
            continue
        try:
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            _dlls_added.add(str(bin_dir))
            added.append(str(bin_dir))
        except OSError:
            pass
    return added


def _ort_cuda(site_dir: Path | None) -> tuple[list, str | None]:
    """ONNX Runtime's CUDA provider for Kokoro, or why not. (providers, reason)."""
    if os.environ.get("KAIRO_TTS_CPU") == "1":
        return ["CPUExecutionProvider"], "set to the CPU (KAIRO_TTS_CPU=1)"
    import onnxruntime as ort
    if "CUDAExecutionProvider" not in ort.get_available_providers():
        return ["CPUExecutionProvider"], "ONNX Runtime is the CPU build (onnxruntime-gpu is not installed)"
    _add_cuda_dlls(site_dir)
    if hasattr(ort, "preload_dlls"):
        try:
            ort.preload_dlls()          # CUDA/cuDNN from the nvidia-* packages next to onnxruntime
        except Exception as exc:   # the session below says whether CUDA really works
            log.info("onnxruntime.preload_dlls: %s", exc)
    # "DEFAULT": no exhaustive cuDNN search per new sentence length (that costs seconds on the first of each)
    return [("CUDAExecutionProvider", {"cudnn_conv_algo_search": "DEFAULT"}), "CPUExecutionProvider"], None


@dataclass
class Transcript:
    text: str
    language: str
    seconds: float
    took: float


@dataclass
class Tuning:
    """The owner's switches for the call path's speech to text (app/flags.py, names g2.<field>). Read once
    when a call connects (Voice.read_flags), never per audio frame. The defaults are the flags' defaults: a
    fix is on, what waits for an owner decision or a measurement on the PC is off."""
    temperature_zero: bool = True
    offline_models: bool = True
    int8_float16: bool = False
    warm_at_connect: bool = True
    single_encode_langid: bool = False


class Voice:
    def __init__(self, models_dir: Path, site_dir: Path | None = None, engine=None):
        self.engine = engine                 # Kairo's database: the owner's g2.* switches are read at start-up (warm)
        self.models_dir = Path(models_dir)
        self.site_dir = Path(os.environ["KAIRO_VOICE_SITE"]) if os.environ.get("KAIRO_VOICE_SITE") else site_dir
        use_site(self.site_dir)
        self._stt = None
        self._stt_device = None
        self._stt_model = None
        self._stt_why: str | None = None     # why Whisper is not on the GPU (None: it is, or not loaded yet)
        self._stt_compute: str | None = None  # the compute type the loaded model really runs in (ctranslate2's word for it)
        self._stt_compute_note: str | None = None   # a compute type that was asked for but did not load
        self._stt_compute_asked: str | None = None  # what _compute_wanted() said when the model was loaded
        self._active = 0                     # speech to text runs going on right now
        self._count = threading.Lock()
        self._last_decode = float("-inf")    # when the last one ended (monotonic)
        self._warming = threading.Lock()     # one warm-up at a time
        self._warm_audio: dict[str, bytes] | None = None
        self._last_language: str | None = None   # the language of the last turn with words (this call)
        self._single_failed: str | None = None   # why the one-pass language finding was given up (None: working)
        self._stt_failed = 0.0
        self._tts_device = None
        self._tts_why: str | None = None
        self.load_ms: dict[str, int] = {}
        self.warm_ms: dict[str, int] = {}
        self._kokoro = None
        self._piper = None
        self._lock = threading.Lock()   # one model load at a time
        self.last_error: str | None = None
        self.tuning = Tuning()

    # ---------------------------------------------------------------- status

    PACKAGES = {"stt": "faster_whisper", "en": "kokoro_onnx", "de": "piper"}

    @staticmethod
    def installed() -> dict[str, bool]:
        from ..tool_sites import is_real_package
        return {k: is_real_package(m)[0] for k, m in Voice.PACKAGES.items()}

    @staticmethod
    def broken() -> list[str]:
        """Packages that are there but can't be imported (e.g. not readable by Kairo's account)."""
        from ..tool_sites import is_real_package
        out = []
        for m in Voice.PACKAGES.values():
            ok, detail = is_real_package(m)
            if not ok and "not installed" not in detail:
                out.append(detail)
        return out

    def status(self) -> dict:
        inst = self.installed()
        return {"installed": inst, "stt_device": self._stt_device, "stt_loaded": self._stt is not None,
                "tts_device": self._tts_device,
                "en_ready": inst["en"] and all((self.models_dir / "kokoro" / f).is_file() for f in KOKORO_FILES),
                "de_ready": inst["de"] and (self.models_dir / "piper" / f"{PIPER_DE}.onnx").is_file(),
                "broken": self.broken(), "last_error": self.last_error}

    def selftest(self) -> dict:
        """For the installer and the boot check (fix P0): real imports as Kairo's account, Kokoro says a
        short sentence (~2 s) and Whisper writes it down again; Piper speaks German. {ok, checks, reason}"""
        checks, problems, heard, notes = {}, [], None, []
        heard_de = german_ok = None
        for key, module in (("stt_import", "faster_whisper"), ("en_import", "kokoro_onnx"), ("de_import", "piper")):
            try:
                mod = __import__(module)
                if module == "faster_whisper":
                    getattr(mod, "WhisperModel")
                checks[key] = True
            except Exception as exc:   # ImportError, the namespace-package case, broken DLLs
                checks[key] = False
                problems.append(f"{module}: {type(exc).__name__}: {exc}"[:240])
        if all(checks.values()):
            try:
                wav, lang = self.speak("Kairo voice test.", "en")
                bad = wav_problems(wav)
                checks["tts_en"] = not bad and lang == "en"
                problems += [f"English voice: {p}" for p in bad]
                t = self.transcribe(wav, language="en")
                heard = t.text
                checks["stt_roundtrip"] = "test" in t.text.lower()
                if not checks["stt_roundtrip"]:
                    problems.append(f"Whisper heard {t.text!r}")
                de, lang = self.speak(GERMAN_TEST, "de")
                bad = wav_problems(de)
                checks["tts_de"] = not bad and lang == "de"
                if lang != "de":
                    problems.append("the German voice didn't speak")
                problems += [f"German voice: {p}" for p in bad]
                if lang == "de" and not bad:
                    heard_de, german_ok = self._german_roundtrip(de)
                    if not german_ok:
                        notes.append(f"Whisper heard the German sentence as {heard_de!r}; the update is not stopped for this")
            except Exception as exc:
                problems.append(f"{type(exc).__name__}: {exc}"[:240])
                checks.setdefault("tts_en", False)
        ok = not problems and all(checks.values())
        return {"ok": ok, "checks": checks, "reason": "; ".join(problems), "heard": heard,
                "heard_de": heard_de, "german_ok": german_ok, "notes": notes,
                "device": self.status().get("stt_device") if heard is not None else None}

    def _german_roundtrip(self, wav: bytes) -> tuple[str | None, bool]:
        """(what Whisper wrote, whether it is the German sentence): through the call path, German given, as a
        German call asks. Reported, not a check: the self-test decides whether an update rolls back, and a
        small model mishearing one sentence is no reason for that. Nothing is learned from it."""
        try:
            text = self.transcribe_pcm(_wav_16k(wav), "de").text
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"[:120], False
        finally:
            self._last_language = None
        return text, any(word in text.lower() for word in GERMAN_TEST_WORDS)

    # ---------------------------------------------------------------- speech to text

    def model_folder(self, name: str) -> Path | None:
        """The downloaded model's folder (Update Kairo put it there), or None. Never raises: the checks use it."""
        found = sorted((self.models_dir / "whisper").glob(f"models--*--faster-whisper-{name}/snapshots/*/model.bin"))
        return found[-1].parent if found else None

    def _local_model(self, name: str) -> str:
        """What WhisperModel loads: the downloaded folder, so loading needs no Hugging Face lookup (network,
        lock files). Not downloaded yet: NotInstalled (the installer downloads, never a call: the model is
        1.5 GB). With g2.offline_models off, the plain name as before, which downloads on the spot."""
        folder = self.model_folder(name)
        if folder is not None:
            return str(folder)
        if self.tuning.offline_models:
            raise NotInstalled(f"The speech model {name} is not downloaded on the PC (run Update Kairo).")
        return name

    def _compute_wanted(self) -> str:
        """The GPU model's compute type: KAIRO_WHISPER_COMPUTE when the owner set it, else int8_float16 when
        g2.int8_float16 is on, else float16 (int8 weights, half-precision maths: ~0.8 GB less graphics memory)."""
        return os.environ.get("KAIRO_WHISPER_COMPUTE", "").strip() or ("int8_float16" if self.tuning.int8_float16 else "float16")

    def _make_stt(self, device: str):
        from faster_whisper import WhisperModel
        root = str(self.models_dir / "whisper")
        offline = self.tuning.offline_models
        note = None
        if device == "cuda":
            _add_cuda_dlls(self.site_dir)
            self._stt_model = WHISPER_GPU
            path, compute = self._local_model(WHISPER_GPU), self._compute_wanted()
            self._stt_compute_asked = compute
            try:
                model = WhisperModel(path, device="cuda", compute_type=compute, download_root=root, local_files_only=offline)
            except Exception as exc:
                if compute == "float16":
                    raise
                # a lighter compute type this GPU or ctranslate2 does not do: float16 on the GPU, not the slow CPU model
                note = f"{compute} did not load ({type(exc).__name__}: {exc}); float16 is used"[:300]
                log.error("Whisper: %s", note)
                compute = "float16"
                model = WhisperModel(path, device="cuda", compute_type=compute, download_root=root, local_files_only=offline)
        else:
            self._stt_model = WHISPER_CPU
            compute = "int8"
            model = WhisperModel(self._local_model(WHISPER_CPU), device="cpu", compute_type=compute, download_root=root,
                                 local_files_only=offline)
        self._stt_compute = getattr(getattr(model, "model", None), "compute_type", None) or compute
        self._stt_compute_note = note
        return model

    def _load_stt(self):
        if self._stt is not None:
            return self._stt
        if not self.installed()["stt"]:
            raise NotInstalled("Speech recognition is not installed on the PC (run Update Kairo).")
        if self._stt_failed and time.monotonic() - self._stt_failed < RETRY_LOAD_AFTER:
            raise NotInstalled(f"Speech recognition could not start on the PC: {self.last_error}")
        t0 = time.monotonic()
        try:
            self._stt, self._stt_device = self._make_stt("cuda"), "cuda"
            self._stt_why = None
        except Exception as exc:   # no GPU / CUDA DLLs missing: CPU, smaller model, and it says why
            self._stt_why = f"{type(exc).__name__}: {exc}"[:300]
            log.error("Whisper on the GPU failed (%s); using the CPU (slow)", self._stt_why)
            try:
                self._stt, self._stt_device = self._make_stt("cpu"), "cpu"
            except Exception as exc2:   # never a bare crash: say why, and don't retry slowly on every sentence
                self._stt_failed = time.monotonic()
                self.last_error = f"load: {type(exc2).__name__}: {exc2}"
                log.error("Whisper could not load: %s", self.last_error)
                raise NotInstalled(f"Speech recognition could not start on the PC: {self.last_error}") from exc2
        self._stt_failed = 0.0
        self.load_ms["whisper"] = int((time.monotonic() - t0) * 1000)
        return self._stt

    def warm(self) -> None:
        """Load the models once, at start, and run each once (CUDA kernels, cuDNN plans), so no turn
        ever waits for a model load. Called in the background when Kairo starts. Whisper runs last, on
        speech made by Kairo's own voice, through the call path. The owner's g2.* switches are read first, so a
        compute type chosen last week is the one the model starts in."""
        self._read_flags_at_start()
        for name, load in (("whisper", self._load_stt), ("kokoro", self._load_kokoro)):
            try:
                with self._lock:
                    load()
            except Exception as exc:   # reported by status(); the request itself will say why
                log.info("voice warm-up: %s", exc)
                continue
            if name == "whisper":
                continue
            t0 = time.monotonic()
            try:
                self.speak("Hi.", "en")
                self.warm_ms[name] = int((time.monotonic() - t0) * 1000)
            except Exception as exc:
                log.warning("voice warm-up run (%s): %s", name, exc)
        if self._stt is not None:
            self._warm_whisper()

    def _read_flags_at_start(self) -> None:
        if self.engine is None:
            return
        try:
            from ..db import session_for
            with session_for(self.engine) as s:
                self.read_flags(s)
        except Exception as exc:   # the defaults stay: a database hiccup must not keep the voice from loading
            log.warning("voice: the g2 switches could not be read at start (%s); the defaults are used", exc)

    def call_started(self, s) -> threading.Thread | None:
        """A call or the voice button connects (s: a database session). The owner's switches are read now, once;
        the last call's language is forgotten; Whisper is warmed in the background (the card is cold after
        minutes of idle) and loaded again if the owner changed the compute type. Returns that thread."""
        self._last_language = None
        try:
            self.read_flags(s)
        except Exception as exc:   # a database hiccup must not stop a call: the last switches stay
            log.warning("voice: the g2 switches could not be read (%s); the last ones stay", exc)
        if not (self.tuning.warm_at_connect or self._compute_changed()):
            return None
        thread = threading.Thread(target=self.warm_call, name="whisper-warm", daemon=True)
        thread.start()
        return thread

    def _compute_changed(self) -> bool:
        return self._stt is not None and self._stt_device == "cuda" and self._compute_wanted() != self._stt_compute_asked

    def warm_call(self) -> None:
        """What call_started runs in the background: load the model again when the compute type changed, then
        warm it. One at a time; never in front of a real sentence."""
        if not self._warming.acquire(blocking=False):
            return
        try:
            with self._lock:
                if self._compute_changed():
                    log.info("Whisper: loading again in %s", self._compute_wanted())
                    self._stt = None
                try:
                    self._load_stt()
                except Exception as exc:   # reported by devices(); the first sentence says why
                    log.info("voice warm-up: %s", exc)
                    return
            if self.tuning.warm_at_connect:
                self._warm_whisper()
        finally:
            self._warming.release()

    def _warm_clips(self) -> dict[str, bytes]:
        """One sentence per language in Kairo's own voices, 16 kHz 16-bit mono; made once. A language whose
        voice is not ready is left out."""
        if not self._warm_audio:
            clips = {}
            for lang, text in WARM_TEXT.items():
                try:
                    wav, said = self.speak(text, lang)
                    if said == lang:
                        clips[lang] = _wav_16k(wav)
                except Exception as exc:
                    log.info("voice warm-up: no %s sentence (%s)", lang, exc)
            self._warm_audio = clips or None
        return self._warm_audio or {}

    def _warm_whisper(self) -> None:
        """Write down each warm sentence the way a call would. Skipped when the card just worked or the owner
        is already talking. Without any voice: a second of quiet noise, so the kernels still run."""
        if time.monotonic() - self._last_decode < WARM_FRESH_S:
            return
        t0 = time.monotonic()
        try:
            clips = self._warm_clips()
            if clips:
                for lang, pcm in clips.items():
                    if self._active:
                        break
                    self._stt_run(lambda pcm=pcm: _samples(pcm), lang, **self._call_options())
            else:
                import numpy as np
                noise = (np.random.default_rng(0).standard_normal(16000) * 0.01).astype(np.float32)
                self._stt_run(lambda: noise, "en", vad_filter=False, **self._call_options())
            self.warm_ms["whisper"] = int((time.monotonic() - t0) * 1000)
        except Exception as exc:
            log.warning("voice warm-up run (whisper): %s", exc)

    def devices(self) -> dict:
        """Owner's request 2026-10-17: where each part of the voice runs, never a silent CPU fallback.
        {component: {device, model, loaded, ok, reason, load_ms, warm_ms}}; ok False = red in Voice speed."""
        inst = self.installed()
        stt_loaded = self._stt is not None
        whisper = {"device": self._stt_device, "model": self._stt_model, "loaded": stt_loaded,
                   "load_ms": self.load_ms.get("whisper"), "warm_ms": self.warm_ms.get("whisper"),
                   "compute_type": self._stt_compute if stt_loaded else None, "compute_note": self._stt_compute_note,
                   "ctranslate2": _ct2_version(),
                   "language_detection": ("one pass, English or German" if self.tuning.single_encode_langid and not self._single_failed
                                          else "Whisper's own (two passes, 99 languages)"),
                   "language_detection_note": self._single_failed}
        if not inst["stt"]:
            whisper.update(ok=False, reason="not installed")
        elif not stt_loaded:
            whisper.update(ok=False, reason=self.last_error or "not loaded yet (it loads when Kairo starts)")
        else:
            whisper.update(ok=self._stt_device == "cuda",
                           reason=None if self._stt_device == "cuda" else f"on the CPU: {self._stt_why or 'no GPU'}")
        kokoro = {"device": self._tts_device, "model": KOKORO_MODEL, "loaded": self._kokoro is not None,
                  "load_ms": self.load_ms.get("kokoro"), "warm_ms": self.warm_ms.get("kokoro")}
        if not inst["en"]:
            kokoro.update(ok=False, reason="not installed")
        elif self._kokoro is None:
            kokoro.update(ok=False, reason="not loaded yet (it loads when Kairo starts)")
        else:
            kokoro.update(ok=self._tts_device == "cuda",
                          reason=None if self._tts_device == "cuda" else f"on the CPU: {self._tts_why or 'no GPU'}")
        piper = {"device": "cpu", "model": PIPER_DE, "loaded": self._piper is not None, "ok": bool(inst["de"]),
                 "reason": None if inst["de"] else "not installed", "note": "German voice: small, on the CPU by design"}
        return {"whisper": whisper, "kokoro": kokoro, "piper": piper}

    def transcribe(self, audio: bytes, language: str | None = None) -> Transcript:
        if not audio:
            raise VoiceError("no audio")
        if len(audio) > MAX_AUDIO:
            raise VoiceError("recording too long")
        return self._stt_run(lambda: io.BytesIO(audio), language, beam_size=5)

    def read_flags(self, s) -> Tuning:
        """The owner's g2.* switches, read once as a call connects (s: a database session)."""
        from .. import flags
        self.tuning = Tuning(**{n[3:]: flags.on(s, n) for n in flags.FLAGS if n.startswith("g2.")})
        return self.tuning

    def transcribe_pcm(self, pcm: bytes, language: str | None = None) -> Transcript:
        """Voice V1: 16 kHz 16-bit mono samples straight from the phone (no file to decode), greedy
        decoding (beam 1): the fast path for a spoken turn. language: "en" or "de" (or any Whisper code)
        decodes in that language; "auto" or None leaves it to Whisper to find (the call's language setting)."""
        if len(pcm) < 3200:   # under 0.1 s
            raise VoiceError("no audio")
        if len(pcm) > MAX_AUDIO:
            raise VoiceError("recording too long")
        fixed = language if language not in (None, "", "auto") else None
        opts = self._call_options()
        if fixed is None and self.tuning.single_encode_langid:     # "English and German": found in the same pass
            tr = self._stt_run(lambda: _samples(pcm), None, detect=True, prev_language=self._last_language, **opts)
        else:
            tr = self._stt_run(lambda: _samples(pcm), fixed, **opts)
        if tr.text and tr.language in langid.LANGUAGES:
            self._last_language = tr.language
        return tr

    def _call_options(self) -> dict:
        """What a spoken turn asks Whisper for (the plan's call path): one greedy decode; no previous-text
        prompt; no timestamps."""
        opts = {"beam_size": 1, "condition_on_previous_text": False, "without_timestamps": True}
        if self.tuning.temperature_zero:
            opts["temperature"] = 0.0      # one decode, no random re-tries (Whisper's default list goes up to 1.0)
        return opts

    def _stt_run(self, audio, language: str | None, vad_filter: bool = True, *, detect: bool = False,
                 prev_language: str | None = None, **opts) -> Transcript:
        """detect: find English or German in the same encoder pass (langid.py) instead of Whisper's own
        language=None, which encodes twice; prev_language: what an unsure turn falls back to."""
        with self._lock:
            model = self._load_stt()
        t = time.monotonic()
        with self._count:
            self._active += 1
        try:
            if detect and not self._single_failed and getattr(getattr(model, "model", None), "is_multilingual", True):
                try:
                    segments, info = langid.transcribe_en_de(model, audio(), prev_language=prev_language,
                                                             vad_filter=vad_filter, **opts)
                except Exception as exc:
                    if self._stt_device == "cuda" and GPU_RUNTIME.search(str(exc)):
                        raise      # the card, not the new path: the CPU fallback below
                    self._single_failed = f"{type(exc).__name__}: {exc}"[:200]
                    log.error("Whisper: one-pass language finding failed (%s); Whisper's own is used", self._single_failed)
                    segments, info = model.transcribe(audio(), language=None, vad_filter=vad_filter, **opts)
            else:
                segments, info = model.transcribe(audio(), language=language, vad_filter=vad_filter, **opts)
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
                return self._stt_run(audio, language, vad_filter, detect=detect, prev_language=prev_language, **opts)
            self.last_error = f"transcribe: {exc}"
            raise VoiceError(f"could not understand the audio ({type(exc).__name__})") from exc
        finally:
            with self._count:
                self._active -= 1
            self._last_decode = time.monotonic()
        return Transcript(text=text, language=info.language, seconds=round(info.duration, 1), took=round(time.monotonic() - t, 2))

    # ---------------------------------------------------------------- text to speech

    def _load_kokoro(self):
        if self._kokoro is None:
            if not self.installed()["en"]:
                raise NotInstalled("The English voice is not installed on the PC.")
            d = self.models_dir / "kokoro"
            if not all((d / f).is_file() for f in KOKORO_FILES):
                raise NotInstalled("The English voice model is missing (run Update Kairo).")
            import onnxruntime as ort
            from kokoro_onnx import Kokoro
            t0 = time.monotonic()
            providers, why = _ort_cuda(self.site_dir)
            try:
                session = ort.InferenceSession(str(d / KOKORO_MODEL), providers=providers)
            except Exception as exc:   # CUDA there but broken: the CPU, and it says why
                why = f"{type(exc).__name__}: {exc}"[:300]
                session = ort.InferenceSession(str(d / KOKORO_MODEL), providers=["CPUExecutionProvider"])
            on_gpu = session.get_providers()[0] == "CUDAExecutionProvider"
            if not on_gpu and why is None:
                why = "ONNX Runtime could not start CUDA (CUDA/cuDNN DLLs not found)"
            self._tts_device, self._tts_why = ("cuda" if on_gpu else "cpu"), (None if on_gpu else why)
            if not on_gpu:
                log.error("Kokoro on the CPU (slow): %s", why)
            self._kokoro = Kokoro.from_session(session, str(d / "voices-v1.0.bin"))
            self.load_ms["kokoro"] = int((time.monotonic() - t0) * 1000)
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

    def speak(self, text: str, language: str | None = None, *, voice: str | None = None,
              speed: float | None = None) -> tuple[bytes, str]:
        """WAV bytes and the language used. Long texts are cut at a sentence end. voice / speed: a Kokoro
        voice other than KOKORO_VOICE, a speaking rate (1.0 = as made); the voice latency harness renders its
        clips with several (app/bench_corpus.py). Calls and replies never pass them."""
        text = _clean_for_speech(text)
        if not text:
            raise VoiceError("nothing to say")
        lang = language or language_of(text)
        with self._lock:
            if lang == "de":
                try:
                    piper_voice = self._load_piper()
                except NotInstalled:
                    lang = "en"   # better an English voice than none
            if lang != "de":
                kokoro = self._load_kokoro()
        if lang == "de":
            buf = io.BytesIO()
            config = None
            if speed:
                from piper import SynthesisConfig
                config = SynthesisConfig(length_scale=1.0 / speed)
            with wave.open(buf, "wb") as w:
                piper_voice.synthesize_wav(text, w, syn_config=config)
            return buf.getvalue(), "de"
        samples, rate = kokoro.create(text, voice=voice or KOKORO_VOICE, speed=speed or 1.0, lang="en-us")
        return to_wav(sound(samples), rate), "en"

    # ---------------------------------------------------------------- downloads (installer)

    def download(self) -> list[str]:
        """Fetch every model once, into models_dir (fix F24: the installer's tools\\voice-<hash>\\models,
        only admins can change it; it used to be data\\models). Returns what was done; marks the folder
        complete at the end, so Kairo only switches to it once every model is there."""
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
        (self.models_dir / COMPLETE).write_text("ok\n", encoding="ascii")
        return done


COMPLETE = ".complete"


def models_dir(data_dir: Path, site_dir: Path | None) -> Path:
    """Fix F24: the models next to the packages (tools\\voice-<hash>\\models) once the installer has
    them all there; until then the old data\\models."""
    if site_dir is not None and (Path(site_dir) / "models" / COMPLETE).is_file():
        return Path(site_dir) / "models"
    return Path(data_dir) / "models"


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
