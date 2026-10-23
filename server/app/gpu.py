"""Where the voice runs (owner's request 2026-10-17): the graphics card's memory, per process when the
driver tells it, and what llama.cpp's own log says about the voice model (CUDA found, layers on the GPU, how
much memory it took, or that the card was full). Also the driver's CUDA sysmem fallback policy (voice recipe 9).
Read-only: nvidia-smi, log files and the driver's setting on this PC; nothing leaves it."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import nvsettings

SMI_TIMEOUT_S = 5
SYSMEM_TTL_S = 120            # the policy changes when the owner or a driver install changes it, not between two page loads
SYSMEM_CHILD_TIMEOUT_S = 20   # a Python start on a busy PC

_OFFLOADED = re.compile(r"offloaded (\d+)/(\d+) layers to GPU")
_CUDA_FOUND = re.compile(r"ggml_cuda_init: found (\d+) CUDA devices?")
# llama.cpp's messages when the card cannot start or is full (ggml-cuda.cu, llama-model.cpp, llama-kv-cache.cpp, llama-context.cpp).
# With the sysmem policy at 1 a full card is one of the last four instead of a quiet slowdown.
_OOM_TEXT = (r"cudaMalloc failed: [^\r\n]*|unable to allocate CUDA\d+ buffer|"
             r"failed to allocate (?:buffer for kv cache|compute \w+ buffers)")
_CUDA_FAILED = re.compile(r"ggml_cuda_init: failed[^\r\n]*|no usable GPU found[^\r\n]*|CUDA error[^\r\n]*|" + _OOM_TEXT, re.I)
_OOM = re.compile(_OOM_TEXT, re.I)
# The server saying it listens means the model and its memory are in: llama.cpp can retry a failed work-space allocation and then start
# (llama-context.cpp, pipeline parallelism off), so a message BEFORE this line is not a dead start. "listening on" is in the old and the new wording.
_LISTENING = re.compile(r"\blistening on\b", re.I)
_BUF = re.compile(r"\b(CUDA\d+)\s+(model|KV|RS|compute) buffer size\s*=\s*([\d.]+)\s*MiB")
_DEVICE = re.compile(r"using device (CUDA\d+) \(([^)]+)\)")
_RUN = re.compile(r"build: \d+")          # every llama-server start logs its build first


def present() -> bool:
    """An NVIDIA driver is installed (nvidia-smi comes with it): the voice is meant to run on the GPU here."""
    return shutil.which("nvidia-smi") is not None


def _smi(args: list[str]) -> list[list[str]] | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, *args, "--format=csv,noheader,nounits"], capture_output=True, text=True,
                             timeout=SMI_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [[c.strip() for c in line.split(",")] for line in out.stdout.strip().splitlines() if line.strip()]


def _int(v: str) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def summary() -> dict | None:
    """{name, driver, used_mb, total_mb, processes: [{pid, name, used_mb}]} for the first GPU, or None
    when there is no NVIDIA driver. Windows often can't say per process (used_mb None then)."""
    rows = _smi(["--query-gpu=name,driver_version,memory.used,memory.total"])
    if not rows:
        return None
    name, driver, used, total = (rows[0] + ["", "", "", ""])[:4]
    procs = []
    for row in _smi(["--query-compute-apps=pid,process_name,used_gpu_memory"]) or []:
        if len(row) >= 3:
            procs.append({"pid": _int(row[0]), "name": Path(row[1]).name[:60], "used_mb": _int(row[2])})
    return {"name": name, "driver": driver, "used_mb": _int(used), "total_mb": _int(total), "processes": procs}


def _newest_start(log_path: Path, tail_bytes: int) -> str | None:
    """The end of llama.cpp's log from its newest start on (None: no log to read)."""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    return _last_run(text)


def _last_run(text: str) -> str:
    runs = [m.start() for m in _RUN.finditer(text)]
    return text[runs[-1]:] if runs else text          # only the newest start counts


def _oom(last: str) -> str | None:
    """llama.cpp's words when the card was full and the start did not get past it (None when there is no such message or the server went
    on to listen after the last one). The first message is the cause ("cudaMalloc failed: out of memory"); the ones after it only say what
    could not be allocated. Weights, context memory and work space can each run out, and the "offloaded n/n layers" line comes between
    the first and the others, so the layer count alone cannot tell a finished start from a dead one."""
    hits = list(_OOM.finditer(last))
    if not hits or _LISTENING.search(last, hits[-1].end()):
        return None
    return hits[0].group(0).strip()[:200]


def llama_device(log_path: Path, *, tail_bytes: int = 400_000) -> dict:
    """What llama.cpp's log says about its last start: {device: gpu|cpu|None, layers, gpu, ok, reason}."""
    out = {"device": None, "layers": None, "gpu": None, "ok": False, "reason": "no log yet (the voice model hasn't started)"}
    last = _newest_start(log_path, tail_bytes)
    if last is None:
        return out
    failed = _CUDA_FAILED.search(last)
    found = _CUDA_FOUND.search(last)
    dev = _DEVICE.search(last)
    offl = list(_OFFLOADED.finditer(last))
    oom = _oom(last)
    if dev:
        out["gpu"] = dev.group(2)[:60]
    if offl:
        n, total = int(offl[-1].group(1)), int(offl[-1].group(2))
        out["layers"] = f"{n}/{total}"
        if n == 0:
            out.update(device="cpu", ok=False, reason=f"0/{total} layers on the GPU" + (f": {failed.group(0).strip()[:200]}" if failed else ""))
        elif oom:       # the weights went on the card, then the context memory or the work space did not fit: the start is dead
            out.update(device="gpu", ok=False, reason=f"{n}/{total} layers on the GPU, then the card ran out of memory: {oom}")
        elif n < total:
            out.update(device="gpu", ok=False, reason=f"only {n}/{total} layers on the GPU (not enough free VRAM?)")
        else:
            out.update(device="gpu", ok=True, reason=None)
        return out
    if failed:
        out.update(device="cpu", ok=False, reason=failed.group(0).strip()[:200])
    elif found:
        out.update(device="gpu", ok=True, reason=None)
    else:
        out.update(reason="the log doesn't say yet (still loading?)")
    return out


def llama_vram_text(text: str) -> dict:
    """{model, kv, rs, compute, total (MiB on the card), oom} from llama-server's log, for its newest start. The numbers
    are None until it has printed them. compute is the largest buffer, not the sum: each reserve prints the same one again."""
    last = _last_run(text)
    parts = {"model": 0.0, "kv": 0.0, "rs": 0.0, "compute": 0.0}
    seen = False
    for _dev, kind, mib in _BUF.findall(last):
        key = {"model": "model", "KV": "kv", "RS": "rs", "compute": "compute"}[kind]
        parts[key] = max(parts[key], float(mib)) if key == "compute" else parts[key] + float(mib)
        seen = True
    said = _oom(last)
    if not seen:
        return {"model": None, "kv": None, "rs": None, "compute": None, "total": None, "oom": said}
    got = {k: round(v) for k, v in parts.items()}
    return {**got, "total": sum(got.values()), "oom": said}


def llama_vram(log_path: Path, *, tail_bytes: int = 400_000) -> dict:
    """llama_vram_text for the log file (all None when there is no log yet)."""
    return llama_vram_text(_newest_start(log_path, tail_bytes) or "")


def free_mb() -> int | None:
    """Free graphics memory in MB (first GPU), from one nvidia-smi query; None when the driver doesn't say."""
    rows = _smi(["--query-gpu=memory.used,memory.total"])
    if not rows or len(rows[0]) < 2:
        return None
    used, total = _int(rows[0][0]), _int(rows[0][1])
    return None if used is None or total is None else total - used


_sysmem_cache: tuple[float, dict] | None = None


def _no_sysmem(why: str) -> dict:
    return {"available": False, "value": None, "off": False, "label": "not available", "why": why[:200]}


def _read_sysmem() -> dict:
    if sys.platform != "win32":
        return nvsettings.status()                   # "Windows only": answers without touching a driver
    cmd = [sys.executable, "-I", str(Path(nvsettings.__file__)), "get", "--json"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=SYSMEM_CHILD_TIMEOUT_S,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return _no_sysmem("the driver check did not answer in time")
    except (OSError, subprocess.SubprocessError) as exc:
        return _no_sysmem(f"the driver check could not start: {exc}")
    try:
        got = json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else None
    except ValueError:
        got = None
    if isinstance(got, dict) and "available" in got:
        return {"available": bool(got["available"]), "value": got.get("value"), "off": bool(got.get("off")),
                "label": str(got.get("label") or ""), "why": got.get("why"), "via": got.get("via")}
    return _no_sysmem(f"the driver check failed (exit {out.returncode})" + (f": {out.stderr.strip()[-120:]}" if out.stderr.strip() else ""))


def sysmem_policy(*, force: bool = False) -> dict:
    """NVIDIA's CUDA sysmem fallback policy as the driver has it: {available, value (0|1|2|None), off (value is 1), label, why, via (which NVAPI ids answered)}.
    Read by a child Python (app/nvsettings.py), never inside Kairo: a driver call that goes wrong can't take the server down.
    Kept for SYSMEM_TTL_S (an answer that failed too, so a page that polls doesn't start a process each time)."""
    global _sysmem_cache
    now = time.monotonic()
    if not force and _sysmem_cache is not None and now - _sysmem_cache[0] < SYSMEM_TTL_S:
        return dict(_sysmem_cache[1])
    got = _read_sysmem()
    _sysmem_cache = (now, got)
    return dict(got)
