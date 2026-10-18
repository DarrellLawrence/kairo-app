"""Where the voice runs (owner's request 2026-10-17): the graphics card's memory, per process when the
driver tells it, and what llama.cpp's own log says about the voice model (CUDA found, layers on the GPU).
Read-only: nvidia-smi and log files on this PC; nothing leaves it."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

SMI_TIMEOUT_S = 5

_OFFLOADED = re.compile(r"offloaded (\d+)/(\d+) layers to GPU")
_CUDA_FOUND = re.compile(r"ggml_cuda_init: found (\d+) CUDA devices?")
_CUDA_FAILED = re.compile(r"ggml_cuda_init: failed[^\n]*|no usable GPU found[^\n]*|CUDA error[^\n]*", re.I)
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


def llama_device(log_path: Path, *, tail_bytes: int = 400_000) -> dict:
    """What llama.cpp's log says about its last start: {device: gpu|cpu|None, layers, gpu, ok, reason}."""
    out = {"device": None, "layers": None, "gpu": None, "ok": False, "reason": "no log yet (the voice model hasn't started)"}
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return out
    runs = [m.start() for m in _RUN.finditer(text)]
    last = text[runs[-1]:] if runs else text          # only the newest start counts
    failed = _CUDA_FAILED.search(last)
    found = _CUDA_FOUND.search(last)
    dev = _DEVICE.search(last)
    offl = list(_OFFLOADED.finditer(last))
    if dev:
        out["gpu"] = dev.group(2)[:60]
    if offl:
        n, total = int(offl[-1].group(1)), int(offl[-1].group(2))
        out["layers"] = f"{n}/{total}"
        if n == 0:
            out.update(device="cpu", ok=False, reason=f"0/{total} layers on the GPU" + (f": {failed.group(0)[:200]}" if failed else ""))
        elif n < total:
            out.update(device="gpu", ok=False, reason=f"only {n}/{total} layers on the GPU (not enough free VRAM?)")
        else:
            out.update(device="gpu", ok=True, reason=None)
        return out
    if failed:
        out.update(device="cpu", ok=False, reason=failed.group(0)[:200])
    elif found:
        out.update(device="gpu", ok=True, reason=None)
    else:
        out.update(reason="the log doesn't say yet (still loading?)")
    return out
