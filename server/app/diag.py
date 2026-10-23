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
import json
import os
import platform
import sys
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
}


@dataclass
class Check:
    id: str                      # "whisper.gpu": stable, for scripts and the PWA
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
        weights = Path(ctx.voice._local_model(WHISPER_GPU)) / "model.bin"     # the downloaded model, else just its name
        size = weights.stat().st_size / 1024 ** 2 if weights.is_file() else None
        first = (ok("whisper.installed", "installed", f"faster-whisper found; model {WHISPER_GPU} ({mb(size)})", round(size), "MB")
                 if size is not None else bad("whisper.installed", "installed", f"the model {WHISPER_GPU} isn't downloaded (run Update Kairo)"))
    return [first] + live_checks(ctx, "whisper", "whisper", gpu_required=True)


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


CHECKS = [_kairo, _whisper, _kokoro, _piper]


# ---------------------------------------------------------------- the report

def collect(*, data_dir: Path, app_root: Path = REPO_ROOT, live: dict | None = None, deep: bool = False,
            notes: list[str] | None = None, source: str = "local") -> dict:
    """All checks, as the dict `kairo diag --json` prints and /api/diag/voice returns (already redacted).
    One check that crashes is a FAIL of its own; the others still run."""
    from . import versions
    ctx = Ctx(Path(data_dir), Path(app_root), live, deep, list(notes or []))
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
                   "checks": [c.as_dict() for c in checks]})


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kairo diag", description="Is Kairo's voice stack healthy? Exit 0 when nothing FAILs.")
    ap.add_argument("--json", action="store_true", help="print the report as JSON (what /api/diag/voice returns)")
    ap.add_argument("--strict", action="store_true", help="a WARN also makes the exit code 1")
    ap.add_argument("--deep", action="store_true", help="also check the sha256 of the multi-GB model files (slow)")
    ap.add_argument("--local", action="store_true", help="don't ask the running Kairo; check only what this process can see")
    ap.add_argument("--env-file", type=Path, help="kairo.env written by the installer (the server's own settings)")
    args = ap.parse_args(argv)
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
        report = finish(report, args.strict)
    except Exception as exc:   # the tool itself is broken: not "unhealthy", a different exit code
        print(f"kairo diag could not run: {type(exc).__name__}: {diagnosis.redact(str(exc))[:300]}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=1) if args.json else render(report))
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
