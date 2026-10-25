"""kairo diag (owner's spec): is the voice stack healthy? One line per check, PASS / WARN / FAIL, a number
where there is one, and the reason for a FAIL in plain words. Exit 0 when nothing FAILs.

  python -m app.diag [--json] [--strict] [--deep] [--local] [--env-file <data>/kairo.env]

On the PC `kairo diag` runs this (installer/bat/kairo.cmd). The same report is what More -> System ->
"Voice check" shows (GET /api/diag/voice, owner only, built by collect()).

Read-only: files are only read and hashed, nvidia-smi / tailscale are only asked, nothing is written
into the data folder, nothing calls a model. Everything that leaves this module goes through
diagnosis.redact(), so the text can be copied or shared as it is. It builds on what already exists:
Voice.installed()/status() (voice/), gpu.py (graphics card, llama.cpp's log), voicellm.py (what the
installer recorded and pinned), tailnet.py, diagnosis.redact; the live parts (loaded, warm, on the GPU)
are the running server's own answer (the same dict as /api/voice/devices).
"""

from __future__ import annotations

import argparse
import datetime as dt
import functools
import importlib
import importlib.util
import ipaddress
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import diagnosis, gpu, tool_sites, voicellm
from . import voice as voice_mod
from .serve import HOST
from .settings import REPO_ROOT

SCHEMA = 1
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
SOURCES = {"server": "asked the running Kairo", "local": "this process only"}
NEEDS_SERVER = "can't tell: Kairo's server didn't answer, so what is loaded and where it runs is unknown"

GROUPS = {
    "kairo": "Kairo",
    "whisper": "Speech to text (Whisper)",
    "kokoro": "English voice (Kokoro)",
    "piper": "German voice (Piper)",
    "llama": "Voice model (llama.cpp)",
    "smart_turn": "Turn detector (Smart Turn)",
    "silero": "Voice activity (Silero VAD)",
    "speaker": "Speaker check (CAM++)",
    "embeddings": "Memory search (embeddings)",
    "system": "This PC",
    "net": "Calls and network",
    "bench": "Latency harness",
}

VRAM_MB = 12 * 1024                    # the RTX 3060 in kairo-home
RAM_MB = 16 * 1024
WARN_AT, FAIL_AT = 0.85, 0.95          # memory in use: getting full / about to run out
FIREWALL_RULE = "Kairo calls (WebRTC, Tailscale only)"     # installer/Kairo.psm1 $script:CallFirewallRule (a test keeps them equal)
LINE_WHY = {"not_installed": "the call packages (pipecat, aiortc) aren't installed", "voice_missing": "speech to text isn't installed",
            "no_tailnet": "this PC has no Tailscale address right now (is Tailscale up?)"}
SNAPSHOT_STALE_S = 15 * 60


@dataclass
class Check:
    id: str                      # "whisper.device": stable, for scripts and the PWA
    group: str
    name: str
    status: str                  # PASS | WARN | FAIL
    detail: str                  # plain words; for a FAIL, the reason
    value: float | int | None = None
    unit: str | None = None

    def as_dict(self) -> dict:
        d = asdict(self)
        for k in ("group", "name", "detail"):
            d[k] = diagnosis.redact(str(d[k]))
        return d


def _status(status: str):
    def make(cid: str, name: str, detail: str, value=None, unit=None, *, group: str | None = None) -> Check:
        return Check(cid, group or GROUPS.get(cid.split(".")[0], cid.split(".")[0]), name, status, detail, value, unit)
    return make


ok, warn, bad = _status(PASS), _status(WARN), _status(FAIL)


def mb(n: float | None) -> str:
    return "?" if n is None else f"{n / 1024:.1f} GB" if n >= 1024 else f"{n:.0f} MB" if n >= 10 else f"{n:.1f} MB"


# ---------------------------------------------------------------- what the checks look at

@dataclass
class Ctx:
    data_dir: Path
    app_root: Path = REPO_ROOT
    live: dict | None = None            # the running server's own state: {"devices": /api/voice/devices, ...}
    deep: bool = False                  # also hash the multi-GB model files
    notes: list[str] = field(default_factory=list)
    bench: dict = field(default_factory=lambda: {"installed": False, "mock": None, "latest": None})
    source: str = "local"               # "server": this report is built inside Kairo's server (its account is the one that reads)

    @functools.cached_property
    def voice(self) -> "voice_mod.Voice":
        site = tool_sites.site("voice", self.data_dir)
        return voice_mod.Voice(voice_mod.models_dir(self.data_dir, site), site_dir=site)

    @functools.cached_property
    def vl_site(self) -> Path:
        return voicellm.site(self.data_dir)

    @functools.cached_property
    def recorded(self) -> dict:
        """voicellm's installed.json: what the installer downloaded, with the sizes and sha256 it checked."""
        return voicellm.installed(self.vl_site)

    def dev(self, key: str) -> dict | None:
        """One row of /api/voice/devices (whisper, kokoro, piper, voice_model, embeddings, speaker)."""
        return (((self.live or {}).get("devices") or {}).get("components") or {}).get(key)

    def rel(self, p: Path) -> str:
        """A path without the user's folders: relative to the Kairo folder (C:\\Kairo) when it is inside."""
        try:
            return str(Path(p).relative_to(self.data_dir.parent))
        except ValueError:
            return Path(p).name


def package(ctx: Ctx, cid: str, what: str, module: str) -> Check | None:
    """None when the package imports; else the FAIL that says why."""
    ctx.voice                      # Voice() puts the voice packages' folder on sys.path: find_spec needs it
    found, why = tool_sites.is_real_package(module)
    return None if found else bad(f"{cid}.installed", "installed", f"{what} isn't usable: {why or module} (run Update Kairo)")


def live_checks(ctx: Ctx, key: str, cid: str, *, gpu_required: bool, lazy: bool = False) -> list[Check]:
    """loaded / warm and where it runs, from the running server's answer. lazy: a part that loads on first use
    (not loaded is a WARN then, not a FAIL). Not on the GPU although it should be is a FAIL with the reason."""
    miss = warn if lazy else bad
    if ctx.live is None:
        return [bad(f"{cid}.loaded", "loaded", NEEDS_SERVER), bad(f"{cid}.device", "where it runs", NEEDS_SERVER)]
    d = ctx.dev(key)
    if d is None:
        return [warn(f"{cid}.loaded", "loaded", "Kairo doesn't report this part on this PC")]
    why = d.get("reason") or d.get("note") or "no reason given"
    if not d.get("loaded"):
        return [miss(f"{cid}.loaded", "loaded", f"not loaded: {why}"),
                miss(f"{cid}.device", "where it runs", "not loaded, so not on the GPU" if gpu_required else "not loaded")]
    warm = d.get("warm_ms")
    if "warm_ms" in d and warm is None:
        loaded = warn(f"{cid}.loaded", "loaded", f"loaded in {d.get('load_ms')} ms but not warmed up: the first turn is slow")
    else:
        bits = [f"loaded in {d['load_ms']} ms" if d.get("load_ms") is not None else "loaded"]
        loaded = ok(f"{cid}.loaded", "loaded", ", ".join(bits + ([f"warm-up run {warm} ms"] if warm is not None else [])), warm, "ms")
    device = str(d.get("device") or "?")
    on_gpu = device in ("cuda", "gpu")
    model = f", model {d['model']}" if d.get("model") else ""
    if on_gpu:
        where = ok(f"{cid}.device", "on the GPU", f"on the GPU ({device}){model}")
    elif gpu_required:
        where = bad(f"{cid}.device", "on the GPU", f"on the {device.upper()}, but it should be on the GPU: {why}")
    else:
        where = ok(f"{cid}.device", "where it runs", f"on the CPU by design{model}")
    return [loaded, where]


# ---------------------------------------------------------------- the voice parts

def _kairo(ctx: Ctx) -> list[Check]:
    if ctx.live is None:
        return [bad("kairo.running", "Kairo is running", "; ".join(ctx.notes) or "Kairo's server didn't answer on this PC. "
                    "Start it (Task Scheduler -> Kairo), wait a minute and run this again.")]
    return [ok("kairo.running", "Kairo is running", "this report comes from the running server")]


def _whisper(ctx: Ctx) -> list[Check]:
    from .voice import WHISPER_GPU
    first = package(ctx, "whisper", "faster-whisper", "faster_whisper")
    if first is None:
        folder = ctx.voice.model_folder(WHISPER_GPU)                        # the downloaded model, else None
        size = (folder / "model.bin").stat().st_size / 1024 ** 2 if folder is not None else None
        first = (ok("whisper.installed", "installed", f"faster-whisper found; model {WHISPER_GPU} ({mb(size)})", round(size), "MB")
                 if size is not None else bad("whisper.installed", "installed", f"the model {WHISPER_GPU} isn't downloaded (run Update Kairo)"))
    return [first] + live_checks(ctx, "whisper", "whisper", gpu_required=True) + _whisper_compute(ctx)


def _whisper_compute(ctx: Ctx) -> list[Check]:
    """How the loaded Whisper really runs (voice plan G2): the compute type, the ctranslate2 version, the way the
    language is found. A lighter type that did not load, or a language finding that broke, is a WARN with its reason
    (the call still works: float16 on the GPU, Whisper's own finding). An older server that does not say adds nothing."""
    d = ctx.dev("whisper")
    if not d or not d.get("loaded") or not d.get("compute_type"):
        return []
    bits = [str(d["compute_type"])] + ([f"ctranslate2 {d['ctranslate2']}"] if d.get("ctranslate2") else []) \
        + ([f"language: {d['language_detection']}"] if d.get("language_detection") else []) \
        + ([f"{d['loops_dropped']} repeating piece{'s' if d['loops_dropped'] != 1 else ''} dropped"] if d.get("loops_dropped") else [])
    notes = [str(n) for n in (d.get("compute_note"), d.get("language_detection_note")) if n]
    if notes:
        return [warn("whisper.compute", "compute type", ", ".join(bits) + ". " + " ".join(notes))]
    return [ok("whisper.compute", "compute type", ", ".join(bits))]


def _kokoro(ctx: Ctx) -> list[Check]:
    from .voice import KOKORO_FILES
    first = package(ctx, "kokoro", "kokoro-onnx", "kokoro_onnx")
    if first is None:
        d = ctx.voice.models_dir / "kokoro"
        missing = [n for n in KOKORO_FILES if not (d / n).is_file() or (d / n).stat().st_size == 0]
        first = (bad("kokoro.installed", "installed", f"voice file missing or empty: {', '.join(missing)} (run Update Kairo)")
                 if missing else
                 ok("kokoro.installed", "installed", "kokoro-onnx found; " + ", ".join(f"{n} ({mb((d / n).stat().st_size / 1024 ** 2)})" for n in KOKORO_FILES)))
    return [first] + live_checks(ctx, "kokoro", "kokoro", gpu_required=True)


def _piper(ctx: Ctx) -> list[Check]:
    from .voice import PIPER_DE
    first = package(ctx, "piper", "piper-tts", "piper")
    if first is None:
        d = ctx.voice.models_dir / "piper"
        missing = [n for n in (f"{PIPER_DE}.onnx", f"{PIPER_DE}.onnx.json") if not (d / n).is_file()]
        first = (bad("piper.installed", "installed", f"voice file missing: {', '.join(missing)} (run Update Kairo)") if missing else
                 ok("piper.installed", "installed", f"piper found; voice {PIPER_DE}"))
    # Piper loads on the first German sentence, so "not loaded yet" is normal; it never needs the GPU
    return [first] + live_checks(ctx, "piper", "piper", gpu_required=False, lazy=True)


# ---------------------------------------------------------------- files, pinned or recorded

def verify(path: Path, *, size: int | None, sha256: str | None, hash_it: bool = True) -> str | None:
    """None when the file is what was pinned (voicellm.json) or recorded (installed.json); else why not."""
    try:
        actual = path.stat().st_size
        if size is not None and actual != size:
            return f"it is {actual} bytes, the pin says {size}"
        if sha256 and hash_it and (got := voicellm._sha256(path)) != sha256.lower():
            return f"its sha256 ({got[:12]}) is not the pinned one ({sha256[:12]}): damaged or replaced"
    except OSError as exc:
        return f"it can't be read ({type(exc).__name__})"
    return None


def pipecat_model(ctx: Ctx, subdir: str, pattern: str) -> Path | None:
    """A model file inside the pipecat package (Silero and Smart Turn ship in its wheel), found without importing it."""
    ctx.voice                      # the voice packages' folder must be on sys.path
    try:
        spec = importlib.util.find_spec("pipecat")
    except (ImportError, ValueError):
        return None
    for base in (spec.submodule_search_locations or []) if spec else []:
        hits = sorted((Path(base) / subdir).glob(pattern))
        if hits:
            return hits[-1]
    return None


def probe_smart_turn(path: Path) -> tuple[int | None, str | None]:
    """(ms, None) when the model loads and answers once (on the CPU, as in a call), else (None, why)."""
    from . import smartturn
    if smartturn.np is None:
        return None, "numpy isn't installed (the voice packages)"
    st = smartturn.SmartTurn(path)
    st.probability(b"\x00\x00" * smartturn.RATE)
    return (st.last_ms, None) if st.last_ms is not None else (None, st.failed or "no answer")


def probe_silero(path: Path) -> tuple[int | None, str | None]:
    """(ms, None) when the VAD model loads on the CPU (a call loads its own copy), else (None, why)."""
    try:
        import onnxruntime as ort
        t0 = time.monotonic()
        ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        return int((time.monotonic() - t0) * 1000), None
    except Exception as exc:   # ImportError, a damaged file
        return None, f"{type(exc).__name__}: {exc}"[:200]


def active_model(ctx: Ctx) -> str | None:
    """The voice model that talks: the server's, else the first installed one."""
    return (ctx.dev("voice_model") or {}).get("model") or next(iter(voicellm.available(ctx.vl_site)), None)


# ---------------------------------------------------------------- the voice model, the turn parts, the speaker, the memory

def _llama(ctx: Ctx) -> list[Check]:
    from .brain import voice_local
    pin, rec, mid = voicellm.manifest(), ctx.recorded, active_model(ctx)
    binary, model = voicellm.server_binary(ctx.vl_site), (voicellm.model_path(ctx.vl_site, mid) if mid else None)
    if not rec:
        first = bad("llama.installed", "installed", "the voice model folder isn't installed (run Update Kairo)")
    elif binary is None or model is None:
        first = bad("llama.installed", "installed", "llama-server is missing" if binary is None else "no voice model file is downloaded (run Update Kairo)")
    elif (rec.get("llama") or {}).get("tag") != pin["llama_cpp"]["tag"]:
        first = bad("llama.installed", "installed", f"llama.cpp {(rec.get('llama') or {}).get('tag')} is installed, this release pins {pin['llama_cpp']['tag']} (run Update Kairo)")
    else:
        r = (rec.get("models") or {}).get(mid) or {}
        why = verify(model, size=r.get("size"), sha256=r.get("sha256"), hash_it=ctx.deep)
        size = model.stat().st_size / 1024 ** 2
        first = (bad("llama.installed", "installed", f"{mid}: {why}") if why else
                 ok("llama.installed", "installed", f"llama.cpp {pin['llama_cpp']['tag']}; {mid} {r.get('quant', '')} ({mb(size)}), size matches"
                    + (", sha256 matches" if ctx.deep else " (--deep also checks the sha256)"), round(size), "MB"))
    port = voice_local.PORT
    up = voice_local.health(port)
    state = ctx.dev("voice_model") or {}
    running = (ok("llama.running", "answers", f"answers on {voice_local.HOST}:{port}" + (f" (state: {state['state']})" if state.get("state") else ""))
               if up else bad("llama.running", "answers", f"nothing answers on {voice_local.HOST}:{port}"
                              + (f"; Kairo says it is {state['state']}: {state.get('reason')}" if state.get("state") else "")))
    props = voice_local.props(port) if up else None
    want = int((voicellm.spec(mid) or {}).get("context", 8192)) if mid else None
    if props is None:
        flags = (warn if up else bad)("llama.flags", "context and flags", "it answers /health but not /props" if up else "not running, so its settings can't be read")
    else:
        n_ctx = (props.get("default_generation_settings") or {}).get("n_ctx")
        slots, template = props.get("total_slots"), bool(props.get("chat_template"))
        wrong = [f"context {n_ctx} tokens, Kairo starts it with {want}"] if want and n_ctx is not None and n_ctx != want else []
        unsure = (["it doesn't say its context size"] if n_ctx is None else []) + ([] if template else ["it shows no chat template (tool calls need one)"]) \
            + ([f"{slots} slots; Kairo uses 1 (-np 1) so the prompt cache stays"] if slots not in (1, None) else [])
        flags = (bad("llama.flags", "context and flags", "; ".join(wrong) + " (restart Kairo)", n_ctx, "tokens") if wrong else
                 warn("llama.flags", "context and flags", "; ".join(unsure), n_ctx, "tokens") if unsure else
                 ok("llama.flags", "context and flags", f"context {n_ctx} tokens, 1 slot, chat template on (-c, -np 1, --jinja)", n_ctx, "tokens"))
    dev = gpu.llama_device(ctx.data_dir / "logs" / "voice-llm.log")
    held = gpu.llama_vram(ctx.data_dir / "logs" / "voice-llm.log")
    took = (f"; it holds {mb(held['total'])} of graphics memory (model {mb(held['model'])}, context {mb(held['kv'] + held['rs'])}, "
            f"work space {mb(held['compute'])})" if held["total"] else "")
    layers = (ok("llama.device", "on the GPU", f"{dev['layers']} layers on the GPU" + (f" ({dev['gpu']})" if dev.get("gpu") else "") + took)
              if dev["ok"] else bad("llama.device", "on the GPU", str(dev["reason"])))
    return [first, running, flags, layers]


def _smart_turn(ctx: Ctx) -> list[Check]:
    """WebRTC calls use pipecat's own copy of the model; the old line uses the one the installer downloaded."""
    own = pipecat_model(ctx, "audio/turn/smart_turn/data", "smart-turn-v3*cpu*.onnx")
    copy = voicellm.smart_turn_path(ctx.vl_site)
    rec = ctx.recorded.get("smart_turn") or {}
    why = verify(copy, size=None, sha256=rec.get("sha256")) if copy else None
    if own is None:
        first = bad("smart_turn.installed", "installed", "pipecat's Smart Turn model isn't there: calls can't tell when you are done (run Update Kairo)")
    elif why:
        first = bad("smart_turn.installed", "installed", f"the downloaded copy {copy.name}: {why}")
    else:
        first = ok("smart_turn.installed", "installed", f"{own.name} ({mb(own.stat().st_size / 1024 ** 2)}, WebRTC calls)"
                   + (f"; {copy.name} checksum ok (old line)" if copy else "; the old line's copy isn't downloaded (it uses the plain quiet rule)"),
                   round(own.stat().st_size / 1024 ** 2), "MB")
    ms, problem = probe_smart_turn(own or copy) if (own or copy) else (None, "no model file")
    loaded = (ok("smart_turn.loaded", "loaded", f"loads and answers in {ms} ms", ms, "ms") if ms is not None else
              bad("smart_turn.loaded", "loaded", f"the model doesn't load or answer: {problem}"))
    return [first, loaded, ok("smart_turn.device", "where it runs", "on the CPU by design (about 8M parameters, tens of ms)")]


def _silero(ctx: Ctx) -> list[Check]:
    path = pipecat_model(ctx, "audio/vad/data", "silero_vad*.onnx")
    if path is None:
        return [bad("silero.installed", "installed", "pipecat's Silero VAD model isn't there: calls can't hear when you talk (run Update Kairo)")]
    ms, problem = probe_silero(path)
    return [ok("silero.installed", "installed", f"{path.name} ({mb(path.stat().st_size / 1024 ** 2)})", round(path.stat().st_size / 1024 ** 2), "MB"),
            ok("silero.loaded", "loaded", f"loads in {ms} ms (every call loads its own copy)", ms, "ms") if ms is not None else
            bad("silero.loaded", "loaded", f"the model doesn't load: {problem}"),
            ok("silero.device", "where it runs", "on the CPU by design (one thread)")]


def _speaker(ctx: Ctx) -> list[Check]:
    from . import speaker as sp
    pin = voicellm.manifest()["speaker"]
    path = voicellm.speaker_path(ctx.vl_site)
    why = verify(path, size=pin.get("size"), sha256=pin.get("sha256")) if path else None
    first = (bad("speaker.installed", "installed", "the speaker model isn't downloaded (run Update Kairo): calls work, but anyone is answered")
             if path is None else bad("speaker.installed", "installed", f"{path.name}: {why} (pinned in voicellm.json)") if why else
             ok("speaker.installed", "installed", f"{path.name} ({mb(path.stat().st_size / 1024 ** 2)}), size and sha256 {pin['sha256'][:12]} match the pin",
                round(path.stat().st_size / 1024 ** 2), "MB"))
    print_ = ctx.data_dir / sp.VOICEPRINT
    taught = print_.is_file() and print_.stat().st_size > 0
    enrolled = (ok("speaker.enrolled", "your voice is enrolled", "your voiceprint is on the PC (encrypted, never shown)") if taught else
                warn("speaker.enrolled", "your voice is enrolled", "no voiceprint yet: teach Kairo your voice (More -> Settings -> Voice); until then the speaker check is off"))
    d = ctx.dev("speaker") or {}
    if d.get("on", taught):
        return [first, *live_checks(ctx, "speaker", "speaker", gpu_required=True), enrolled]
    # the check is off (not taught, or switched off): nothing is meant to be loaded, so "enrolled" says it once
    return [first, enrolled] + ([warn("speaker.loaded", "loaded", f"the check is off: {d.get('note') or 'switched off'}")] if taught else [])


def _embeddings(ctx: Ctx) -> list[Check]:
    from . import semantic
    if not tool_sites.configured("memory"):
        return [warn("embeddings.installed", "installed", "semantic memory isn't set up for this release: search finds keywords only")]
    site = tool_sites.site("memory", ctx.data_dir)
    first = (ok("embeddings.installed", "installed", f"packages and model found in {ctx.rel(site)}") if semantic.Embedder(site / "model", site_dir=site).installed()
             else bad("embeddings.installed", "installed", f"packages or model missing in {ctx.rel(site)} (run Update Kairo)"))
    # the model starts with the first memory search: not started yet is normal, on the CPU is not
    return [first, *live_checks(ctx, "embeddings", "embeddings", gpu_required=True, lazy=True)]


# ---------------------------------------------------------------- this PC: memory, environment, firewall, network

def level(used: float, total: float) -> str:
    return FAIL if used >= FAIL_AT * total else WARN if used >= WARN_AT * total else PASS


def ram() -> tuple[int, int] | None:
    """(used, total) MB of this PC's memory, None when the OS doesn't say. No package: ctypes on Windows, /proc on Linux."""
    try:
        if sys.platform == "win32":
            import ctypes

            class Status(ctypes.Structure):          # MEMORYSTATUSEX
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("avail", ctypes.c_ulonglong), ("page_total", ctypes.c_ulonglong), ("page_avail", ctypes.c_ulonglong),
                            ("virt_total", ctypes.c_ulonglong), ("virt_avail", ctypes.c_ulonglong), ("ext", ctypes.c_ulonglong)]
            st = Status()
            st.length = ctypes.sizeof(st)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return None
            return int((st.total - st.avail) >> 20), int(st.total >> 20)
        info = {k: int(v.split()[0]) for k, v in (ln.split(":", 1) for ln in Path("/proc/meminfo").read_text().splitlines() if ":" in ln)}
        return (info["MemTotal"] - info["MemAvailable"]) // 1024, info["MemTotal"] // 1024
    except (OSError, KeyError, ValueError, AttributeError):
        return None


def _system(ctx: Ctx) -> list[Check]:
    card = ((ctx.live or {}).get("devices") or {}).get("gpu") or gpu.summary()
    if not card or card.get("used_mb") is None or not card.get("total_mb"):
        vram = bad("system.vram", "graphics memory (VRAM)", "the graphics card doesn't answer (no NVIDIA driver, or this account can't run nvidia-smi)")
    else:
        used, total = card["used_mb"], card["total_mb"]
        who = "; ".join(f"{p.get('kairo') or p['name']} {mb(p['used_mb'])}" for p in (card.get("processes") or []) if p.get("used_mb"))
        status = WARN if total < 0.9 * VRAM_MB and level(used, total) == PASS else level(used, total)
        vram = Check("system.vram", GROUPS["system"], "graphics memory (VRAM)", status,
                     f"{mb(used)} of {mb(total)} used ({round(100 * used / total)}%); Kairo is sized for {mb(VRAM_MB)}"
                     + (f" and this card has {mb(total)}" if total < 0.9 * VRAM_MB else "") + (f"; {who}" if who else ""), used, "MB")
    mem = ram()
    memory = (warn("system.ram", "memory (RAM)", "this OS doesn't say how much memory is used") if mem is None else
              Check("system.ram", GROUPS["system"], "memory (RAM)", level(*mem), f"{mb(mem[0])} of {mb(mem[1])} used ({round(100 * mem[0] / mem[1])}%); "
                    f"Kairo is sized for {mb(RAM_MB)}", mem[0], "MB"))
    env = (ctx.live or {}).get("env") or {}
    value = str(env.get("HF_HUB_OFFLINE") or "").strip().lower()
    offline = (warn("system.hf_offline", "HF_HUB_OFFLINE", "can't see Kairo's environment: Kairo isn't answering") if ctx.live is None else
               ok("system.hf_offline", "HF_HUB_OFFLINE", "HF_HUB_OFFLINE is set: loading a model never waits for Hugging Face") if value in ("1", "true", "yes", "on") else
               warn("system.hf_offline", "HF_HUB_OFFLINE", "HF_HUB_OFFLINE isn't set for Kairo's voice: a model load can wait for Hugging Face when the network is down"))
    return [vram, memory, offline, _sysmem(ctx, card, vram)]


def _sysmem(ctx: Ctx, card: dict | None, vram: Check) -> Check:
    """NVIDIA's CUDA sysmem fallback policy (voice recipe 9). At 1 a full graphics card is an out-of-memory error; otherwise the
    driver may quietly put CUDA memory into shared memory and the voice gets 2 to 28 times slower with no error anywhere. Setting it
    is the owner's choice (the installer does it only with -CudaSysmemPolicy), so not set is a warning only while the card is nearly full."""
    name = "CUDA sysmem fallback"
    answers = bool(card) and card.get("used_mb") is not None and bool(card.get("total_mb"))
    served = bool(card and card.get("sysmem"))
    pol = card["sysmem"] if served else gpu.sysmem_policy()
    seen = "read by Kairo's server" if served or ctx.source == "server" else "read by this command"
    if pol.get("via"):
        seen += f"; NVAPI {pol['via']}"          # which of the driver's ids answered: what P17 has to see
    if not pol.get("available"):
        if sys.platform != "win32":
            return ok("system.sysmem", name, "not applicable: it is NVIDIA's Windows driver setting, and this PC isn't Windows")
        if not answers:
            return ok("system.sysmem", name, "nothing to judge: no NVIDIA graphics card answers on this PC")
        return warn("system.sysmem", name, f"can't read the driver's setting ({seen}): {pol.get('why') or 'no reason given'}")
    value = pol.get("value")
    if pol.get("off"):
        return ok("system.sysmem", name, f"off (policy 1, {seen}): a full graphics card is an out-of-memory error, never a silent slowdown", value)
    now = f"{pol.get('label') or 'not set'} ({seen})"
    if vram.status in (WARN, FAIL):
        return warn("system.sysmem", name, f"{now}, and the graphics memory is nearly full: the driver may spill into shared memory, a voice "
                    "2 to 28 times slower that nothing reports. Your choice: run Install Kairo with -CudaSysmemPolicy (an administrator window)", value)
    return ok("system.sysmem", name, f"{now}: the driver may spill into shared memory when the card is full. Your choice to change "
              "(Install Kairo -CudaSysmemPolicy)", value)


def firewall_rule() -> dict | None:
    """The call rule as Windows Firewall has it: {found, enabled, action, direction, remote, protocol, program}, else None."""
    script = ("try { $r = @(Get-NetFirewallRule -DisplayName $env:KAIRO_FW_RULE -ErrorAction Stop) } "
              "catch { if ($_.CategoryInfo.Category -eq 'ObjectNotFound') { '{\"found\":false}'; exit 0 } else { exit 3 } }; "
              "[pscustomobject]@{ found = $true; enabled = '' + $r[0].Enabled; action = '' + $r[0].Action; direction = '' + $r[0].Direction; "
              "remote = @($r | Get-NetFirewallAddressFilter | ForEach-Object { $_.RemoteAddress }); "
              "protocol = @($r | Get-NetFirewallPortFilter | ForEach-Object { $_.Protocol }); "
              "program = @($r | Get-NetFirewallApplicationFilter | ForEach-Object { $_.Program }) } | ConvertTo-Json -Compress")
    exe = shutil.which("powershell") or "powershell.exe"
    try:
        out = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=20,
                             env={**os.environ, "KAIRO_FW_RULE": FIREWALL_RULE}, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _list(v) -> list[str]:
    return [str(x) for x in v] if isinstance(v, list) else [str(v)] if v else []


def firewall_check() -> Check:
    from .voicertc import TAILNET
    name = "Windows Firewall rule for calls"
    if sys.platform != "win32":
        return ok("net.firewall", name, "not applicable: the rule belongs to Windows Firewall, and this PC isn't Windows")
    rule = firewall_rule()
    if rule is None:
        return warn("net.firewall", name, "can't read the firewall rule as this account: run kairo diag as an administrator to check it")
    if not rule.get("found"):
        return bad("net.firewall", name, f"there is no rule '{FIREWALL_RULE}': the phone's audio can't reach this PC (run Update Kairo as administrator)")
    nets = [ipaddress.ip_network(t) for t in TAILNET]
    outside = []
    for addr in _list(rule.get("remote")):
        try:       # Windows may write 100.64.0.0/10 as 100.64.0.0/255.192.0.0
            net = ipaddress.ip_network(addr, strict=False)
            if not any(net.version == t.version and net.subnet_of(t) for t in nets):
                outside.append(addr)
        except ValueError:
            outside.append(addr)
    programs = [os.path.normcase(p) for p in _list(rule.get("program"))]
    wrong = ([f"it is switched off ({rule.get('enabled')})"] if str(rule.get("enabled")).lower() not in ("true", "1") else []) \
        + ([f"its action is {rule.get('action')}, not Allow"] if str(rule.get("action")).lower() not in ("allow", "2") else []) \
        + ([f"it also lets in {', '.join(outside)}: not only Tailscale"] if outside or not rule.get("remote") else []) \
        + (["it is for another Python than the one Kairo runs (run Update Kairo)"]
           if programs and os.path.normcase(getattr(sys, "_base_executable", sys.executable)) not in programs else [])
    if wrong:
        return bad("net.firewall", name, "the rule is wrong: " + "; ".join(wrong))
    if [x.upper() for x in _list(rule.get("protocol"))] != ["UDP"]:
        return warn("net.firewall", name, f"the rule is for {', '.join(_list(rule.get('protocol'))) or 'no protocol'}; calls need UDP only")
    return ok("net.firewall", name, "on, inbound, UDP only, from Tailscale's addresses only, for Kairo's Python")


def _network(ctx: Ctx) -> list[Check]:
    from . import tailnet
    line = ((ctx.live or {}).get("devices") or {}).get("line") or {}
    path_name = "Tailscale path to the phone"
    if ctx.live is None:
        calls = bad("net.line", "calls over WebRTC", NEEDS_SERVER)
    elif line.get("webrtc_ready"):
        calls = ok("net.line", "calls over WebRTC", "calls go over WebRTC" + (f" (last call turn: {line['last_turn']})" if line.get("last_turn") else ""))
    else:
        calls = bad("net.line", "calls over WebRTC", f"calls use the old line (slower): {LINE_WHY.get(line.get('reason'), line.get('reason') or 'no reason given')}")
    st = tailnet.status(ctx.data_dir)
    online = [p for p in (st or {}).get("peers") or [] if p.get("online")]
    phones = [p for p in online if str(p.get("os", "")).lower() in ("ios", "android")] or online
    old = f" (snapshot {int((time.time() - st['at']) / 60)} min old)" if st and st.get("source") == "snapshot" and time.time() - st.get("at", 0) > SNAPSHOT_STALE_S else ""
    if st is None:
        path = warn("net.tailscale", path_name, "Tailscale's status isn't available: this account can't run tailscale and the PC's snapshot (status\\tailnet.json) isn't there")
    elif not phones:
        path = warn("net.tailscale", path_name, "no other device is online on the tailnet, so direct or relayed can't be told" + old)
    else:
        relayed = [p for p in phones if not p.get("direct")]
        via = ", ".join(sorted({str(p.get("relay") or st.get("self_relay") or "relay") for p in relayed}))
        path = (warn("net.tailscale", path_name, f"{len(relayed)} of {len(phones)} device(s) go through Tailscale's relay ({via}): tens of ms more each way" + old, len(relayed), "relayed")
                if relayed else ok("net.tailscale", path_name, f"{len(phones)} device(s) online, all direct" + old, 0, "relayed"))
    return [calls, path, firewall_check()]


# ---------------------------------------------------------------- the latency harness (app/bench_voice.py)

MOCK_TIMEOUT_S = 30
STALE_DAYS = 14


def _within(fn, seconds: float):
    """(result, None) or (None, why): fn on a thread that is given up on after `seconds`."""
    box: dict = {}

    def go():
        try:
            box["ok"] = fn()
        except Exception as exc:   # whatever the harness does wrong is its report, not a crash of ours
            box["err"] = f"{type(exc).__name__}: {exc}"[:200]
    t = threading.Thread(target=go, name="diag-bench", daemon=True)
    t.start()
    t.join(seconds)
    return (box["ok"], None) if "ok" in box else (None, box.get("err") or f"no answer after {seconds:g} s")


def _redacted(obj, skip=("git_sha",)):
    """Every string in a harness result through diagnosis.redact (a git sha is long and opaque, and is not a secret)."""
    if isinstance(obj, str):
        return diagnosis.redact(obj)
    if isinstance(obj, list):
        return [_redacted(x, skip) for x in obj]
    if isinstance(obj, dict):
        return {k: v if k in skip else _redacted(v, skip) for k, v in obj.items()}
    return obj


def _bench(ctx: Ctx) -> list[Check]:
    from . import versions
    try:
        harness = importlib.import_module(f"{__package__}.bench_voice")
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}.bench_voice":
            return [bad("bench.mock", "self-check", f"the harness can't be imported: {exc}")]
        return [warn("bench.mock", "self-check", "harness not installed"), warn("bench.latest", "latest live run", "harness not installed")]
    except Exception as exc:
        return [bad("bench.mock", "self-check", f"the harness can't be imported: {type(exc).__name__}: {exc}"[:240])]
    ctx.bench["installed"] = True
    with tempfile.TemporaryDirectory(prefix="kairo-diag-") as tmp:        # nothing is written into the data folder
        res, why = _within(lambda: harness.run(mock=True, url=None, out_dir=Path(tmp)), MOCK_TIMEOUT_S)
    s = (res or {}).get("summary") if isinstance(res, dict) else None
    if why or not (isinstance(s, dict) and res.get("schema") == SCHEMA and res.get("turns")):
        ctx.bench["mock"] = {"ok": False, "error": why or "the result isn't in the agreed shape"}
        mock = bad("bench.mock", "self-check", f"the harness' mock run failed: {why or 'its result is not in the agreed shape (schema 1, turns, summary)'}")
    else:
        ctx.bench["mock"] = {"ok": True, "turns": s.get("n")}
        line = f"mock run works: {s.get('n')} turns, p50 {s.get('p50_ms')} ms, p95 {s.get('p95_ms')} ms (not a measurement)"
        mock = (ok if s.get("pass") else warn)("bench.mock", "self-check", line if s.get("pass") else line + ", but even the mock misses the target", s.get("n"), "turns")
    latest, why = _within(lambda: harness.latest(ctx.data_dir / "bench" / "results"), 10)
    summary = (latest or {}).get("summary") if isinstance(latest, dict) else None
    if why:
        last = bad("bench.latest", "latest live run", f"the results can't be read: {why}")
    elif latest is None:
        last = warn("bench.latest", "latest live run", "no run yet: run the latency harness on the PC (it keeps its results in bench\\results)")
    elif not isinstance(summary, dict):
        last = bad("bench.latest", "latest live run", "the newest result file has no summary")
    elif latest.get("mode") != "live":
        ctx.bench["latest"] = _redacted({k: v for k, v in latest.items() if k != "turns"})
        last = warn("bench.latest", "latest live run", "the newest result is a mock run: no live measurement yet")
    else:
        ctx.bench["latest"] = _redacted({k: v for k, v in latest.items() if k != "turns"})
        p95, target = summary.get("p95_ms"), summary.get("target_p95_ms", 1000)
        stats = f"p50 {summary.get('p50_ms')} ms, p90 {summary.get('p90_ms')} ms, p95 {p95} ms over {summary.get('n')} turns"
        age = None
        try:
            then = dt.datetime.fromisoformat(str(latest.get("at")).replace("Z", "+00:00"))
            age = (dt.datetime.now(dt.timezone.utc) - (then if then.tzinfo else then.replace(tzinfo=dt.timezone.utc))).days
        except ValueError:
            pass
        running = versions.running(ctx.app_root)
        old = [f"{age} days old"] if age is not None and age > STALE_DAYS else []
        old += [f"made on {latest.get('version')}, this is {running}"] if running and latest.get("version") not in (None, running) else []
        last = (bad("bench.latest", "latest live run", f"{stats}: p95 is over the {target} ms target", p95, "ms") if not summary.get("pass") else
                warn("bench.latest", "latest live run", f"{stats}, within the {target} ms target, but {' and '.join(old)}: run it again", p95, "ms") if old else
                ok("bench.latest", "latest live run", f"{stats}: within the {target} ms target", p95, "ms"))
    return [mock, last]


CHECKS = [_kairo, _whisper, _kokoro, _piper, _llama, _smart_turn, _silero, _speaker, _embeddings, _system, _network, _bench]


# ---------------------------------------------------------------- the report

def collect(*, data_dir: Path, app_root: Path = REPO_ROOT, live: dict | None = None, deep: bool = False,
            notes: list[str] | None = None, source: str = "local") -> dict:
    """All checks, as the dict `kairo diag --json` prints and /api/diag/voice returns (already redacted).
    One check that crashes is a FAIL of its own; the others still run."""
    from . import versions
    ctx = Ctx(Path(data_dir), Path(app_root), live, deep, list(notes or []), source=source)
    checks: list[Check] = []
    for fn in CHECKS:
        try:
            checks += fn(ctx)
        except Exception as exc:   # a broken check must not hide the rest
            name = fn.__name__.lstrip("_")
            checks.append(bad(f"{name}.error", "check", f"the check itself crashed: {type(exc).__name__}: {exc}"[:240],
                              group=GROUPS.get(name, name)))
    now = dt.datetime.now(dt.timezone.utc)
    return finish({"schema": SCHEMA, "at": now.isoformat(timespec="seconds"), "source": source,
                   "host": {"version": versions.running(ctx.app_root) or "dev checkout", "python": platform.python_version(),
                            "platform": f"{platform.system()} {platform.release()}"},
                   "checks": [c.as_dict() for c in checks], "bench": ctx.bench})


def recheck_here(report: dict) -> dict:
    """The server runs as Kairo's limited account, which may not be allowed to read Windows Firewall; whoever
    runs `kairo diag` (an administrator) may. A WARN for what the server couldn't see is judged again here."""
    again = {"net.firewall": firewall_check}
    return {**report, "checks": [again[c["id"]]().as_dict() if c["id"] in again and c["status"] == WARN else c for c in report["checks"]]}


def finish(report: dict, strict: bool = False) -> dict:
    """The verdict (counts, ok, exit_code) from the checks themselves, never from a field someone else filled in."""
    n = {s.lower(): sum(c["status"] == s for c in report["checks"]) for s in (PASS, WARN, FAIL)}
    code = 1 if n["fail"] or (strict and n["warn"]) else 0
    return {**report, "counts": n, "ok": code == 0, "exit_code": code}


def fetch(port: int, deep: bool = False) -> tuple[dict | None, str | None]:
    """(the running server's own report, None) or (None, why not). The only thing this module sends anywhere:
    one GET to Kairo's own server on 127.0.0.1 (HOST is fixed, no proxy; outbound.MODULES lists it)."""
    import httpx
    url = f"http://{HOST}:{port}/api/diag/voice"
    try:
        r = httpx.get(url, params={"deep": "true"} if deep else None, timeout=900 if deep else 90, trust_env=False)
    except httpx.HTTPError as exc:
        return None, f"Kairo's server didn't answer on {HOST}:{port} ({type(exc).__name__}). Start it (Task Scheduler -> Kairo) and run this again."
    if r.status_code != 200:
        return None, f"Kairo's server answered {r.status_code} on {HOST}:{port}" + (" (this version has no voice check yet: update it)" if r.status_code == 404 else "")
    try:
        report = r.json()
        assert report["schema"] == SCHEMA and isinstance(report["checks"], list)
    except (ValueError, KeyError, AssertionError, TypeError):
        return None, "Kairo's server sent a report this version can't read (update one of them)"
    return report, None


def render(report: dict) -> str:
    """The plain-text lines (ASCII only: a Windows console may not be UTF-8)."""
    h, n = report["host"], report["counts"]
    lines = [f"Kairo voice check {report['at']}  (version {h['version']}, python {h['python']}, {h['platform']}; "
             f"{SOURCES.get(report['source'], report['source'])})"]
    group = None
    width = max((len(c["id"]) for c in report["checks"]), default=0)
    for c in report["checks"]:
        if c["group"] != group:
            group = c["group"]
            lines += ["", group]
        lines.append(f"  {c['status']:<4}  {c['id']:<{width}}  {c['detail']}")
    verdict = "HEALTHY" if report["ok"] else "NOT HEALTHY"
    lines += ["", f"{n['pass']} PASS, {n['warn']} WARN, {n['fail']} FAIL: {verdict} (exit {report['exit_code']})"]
    return "\n".join(lines).encode("ascii", "replace").decode("ascii")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="kairo diag", description="Is Kairo's voice stack healthy? Exit 0 when nothing FAILs.")
    ap.add_argument("--json", action="store_true", help="print the report as JSON (what /api/diag/voice returns)")
    ap.add_argument("--strict", action="store_true", help="a WARN also makes the exit code 1")
    ap.add_argument("--deep", action="store_true", help="also check the sha256 of the multi-GB model files (slow)")
    ap.add_argument("--local", action="store_true", help="don't ask the running Kairo; check only what this process can see")
    ap.add_argument("--env-file", type=Path, help="kairo.env written by the installer (the server's own settings)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    try:
        notes: list[str] = []
        if args.env_file:
            from .serve import load_env_file
            try:
                load_env_file(args.env_file)
            except OSError as exc:
                notes.append(f"couldn't read the settings file ({type(exc).__name__})")
        from .settings import load_settings
        settings = load_settings()
        report = None
        if args.local:
            notes.append("checked with --local: the running Kairo wasn't asked")
        else:
            report, why = fetch(settings.port, args.deep)
            notes += [why] if why else []
        if report is None:
            report = collect(data_dir=settings.data_dir, deep=args.deep, notes=notes)
        report = finish(recheck_here(report) if report["source"] == "server" else report, args.strict)
    except Exception as exc:   # the tool itself is broken: not "unhealthy", a different exit code
        print(f"kairo diag could not run: {type(exc).__name__}: {diagnosis.redact(str(exc))[:300]}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=1) if args.json else render(report))
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
