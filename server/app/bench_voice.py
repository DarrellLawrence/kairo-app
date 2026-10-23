"""The voice latency harness (owner's spec): how long after you stop talking does Kairo's voice start, step by
step, on the real call pipeline.

A fake phone (aiortc) calls Kairo over WebRTC like the iPhone does: it posts an SDP offer to /api/rtc/offer,
plays each clip of the corpus (app/bench_corpus.py, bench/voice/corpus.json) at real time, then silence, and
records the reply audio. Per turn: total_ms = the clip's true end of speech -> the first reply frame that is
audible at the client (RMS >= 0.01 like the phone's "playing" mark). The stages come from the server's own
numbers for that turn (the data channel's "timing" event, the same `server` dict the phone sends to
app/turns.py, which turns.steps() turns into steps; the first one after the clip began, as the clips are played one at
a time on a quiet line); what is left over is "network".

    python -m app.bench_voice [--mock] [--url http://127.0.0.1:8000] [--clips N] [--out DIR]   (in server/)
    bench_voice.run(mock, url, out_dir) -> results dict      bench_voice.latest(results_dir) -> newest valid results or None

Rates: false_interrupt_rate (a backchannel or noise clip is played while Kairo is talking: did it stop?),
missed_turn_rate (a real utterance got no reply within 5 s), speaker_false_accept_rate (an other_speaker
clip answered although the speaker check is on: null when the check is off or has no voiceprint, as the
check's per-turn label is the only evidence). Results: one JSON per run in <data>/bench/results/ (the schema is
validate() below and bench/results/README.md), with pass = p95 <= 1000 ms, no missed turn, false interrupts <= 5 %.

--mock runs the same client against the same Pipecat pipeline (Silero VAD, Smart Turn) in this process, with
fake speech to text, voice model and voice that have fixed, known delays, so the harness itself can be tested
without models, GPU or a running Kairo.

Measured at 20 ms frames: the speech end and the reply onset are good to about 20 ms (Windows timers add up
to 16 ms). The network stage also holds the difference between the VAD's idea of the end of speech and the
true one, and it is never negative (turns.steps clamps it).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import bench_corpus as bc
from . import gpu, outbound, turns, versions, voicertc
from .bench_corpus import ANSWERED, PROBES
from .latency import pct
from .settings import REPO_ROOT, get_settings
from .voice import KOKORO_MODEL, KOKORO_VOICE, PIPER_DE
from .voicertc import TURN_MAX_SILENCE_S, VAD_STOP_S

SCHEMA = 1
TARGET_P95_MS = 1000
FALSE_INTERRUPT_MAX = 0.05
MISSED_AFTER_S = 5.0                 # a real utterance with no reply by then is a missed turn
STAGES = ("vad_stop", "turn_decision", "stt_final", "llm_first_token", "tts_first_audio", "network")
MODES = ("live", "mock")
PREFIX = "voice-"

TOP_KEYS = {"schema", "version", "git_sha", "at", "mode", "machine", "config", "turns", "summary"}
MACHINE_KEYS = {"gpu", "vram_gb", "ram_gb", "cpu"}
CONFIG_KEYS = {"vad_stop_s", "turn_analyzer", "stt_model", "stt_compute_type", "llm_model", "tts_en", "tts_de"}
TURN_KEYS = {"clip", "lang", "kind", "stages_ms", "total_ms", "interrupted", "missed", "speaker_accept"}
SUMMARY_KEYS = {"n", "p50_ms", "p90_ms", "p95_ms", "by_stage", "false_interrupt_rate", "missed_turn_rate",
                "speaker_false_accept_rate", "target_p95_ms", "pass"}


class BenchError(RuntimeError):
    """The run can't be made (no Kairo to call, no clips); the message says what to do."""


# ---------------------------------------------------------------- one turn

def stages_from_server(server: dict | None, total_ms: float | None) -> dict:
    """The six stages of one turn (ms, None where unknown) from the server's numbers for it and the
    client's total, with turns.steps() doing the arithmetic the Voice speed page uses:
      vad_stop         the VAD's pause before it calls the speech over (VAD_STOP_S)
      stt_final        Whisper's time for the speech (it runs inside the pause)
      turn_decision    what the turn waited after that: Smart Turn or the long pause
      llm_first_token  turn decided -> the voice model's (or Claude's) first word
      tts_first_audio  first word -> first audio sent (0 when the opener came from the cache)
      network          the rest to the client: transport, jitter buffer, the VAD's end-of-speech error
    """
    out = dict.fromkeys(STAGES)
    server = server if isinstance(server, dict) else {}
    if total_ms is None or "first_audio_at_ms" not in server:
        return out
    st = turns.steps({"playing": round(total_ms)}, server, "call")
    vad = server.get("vad_ms")
    stt = st.get("stt_ms")
    out["vad_stop"] = vad if isinstance(vad, (int, float)) else None
    out["stt_final"] = stt
    if "endpoint_ms" in st and out["vad_stop"] is not None:
        out["turn_decision"] = max(0, st["endpoint_ms"] - out["vad_stop"] - (stt or 0))
    out["llm_first_token"] = st.get("llm_ms")
    out["tts_first_audio"] = st.get("tts_ms")
    out["network"] = st.get("net_ms")
    return out


def turn_record(clip, *, stages: dict | None = None, total_ms: float | None = None, interrupted: bool | None = None,
                missed: bool = False, speaker_accept: bool | None = None) -> dict:
    """One entry of "turns", exactly the schema's fields. clip: bench_corpus.Clip."""
    stages = stages or {}
    return {"clip": clip.id, "lang": clip.lang, "kind": clip.kind,
            "stages_ms": {k: (None if stages.get(k) is None else round(stages[k])) for k in STAGES},
            "total_ms": None if total_ms is None else round(total_ms),
            "interrupted": interrupted, "missed": bool(missed), "speaker_accept": speaker_accept}


# ---------------------------------------------------------------- the numbers

def _rate(hits: int, of: int) -> float | None:
    return round(hits / of, 4) if of else None


def summarize(turns_: list[dict]) -> dict:
    """The summary block. Percentiles (latency.pct, as Voice speed) are over the turns that got a reply; n is
    how many that is. A real utterance that the speaker check refused although a synthetic voice is no
    owner (speaker_accept False, not missed) is no missed turn: only the owner's own recordings refused are."""
    totals = [t["total_ms"] for t in turns_ if t["total_ms"] is not None]
    by_stage = {}
    for s in STAGES:
        vals = [t["stages_ms"][s] for t in turns_ if t["stages_ms"].get(s) is not None]
        if vals:
            by_stage[s] = {"p50": pct(vals, .5), "p95": pct(vals, .95)}
    real = [t for t in turns_ if t["kind"] in ANSWERED and not (t["speaker_accept"] is False and not t["missed"])]
    probes = [t for t in turns_ if t["kind"] in PROBES and t["interrupted"] is not None]
    strangers = [t for t in turns_ if t["kind"] == "other_speaker" and t["speaker_accept"] is not None]
    p95 = pct(totals, .95)
    missed = _rate(sum(t["missed"] for t in real), len(real)) or 0.0
    false_interrupt = _rate(sum(bool(t["interrupted"]) for t in probes), len(probes))
    return {"n": len(totals), "p50_ms": pct(totals, .5), "p90_ms": pct(totals, .9), "p95_ms": p95, "by_stage": by_stage,
            "false_interrupt_rate": false_interrupt, "missed_turn_rate": missed,
            "speaker_false_accept_rate": _rate(sum(bool(t["speaker_accept"]) for t in strangers), len(strangers)),
            "target_p95_ms": TARGET_P95_MS,
            "pass": p95 is not None and p95 <= TARGET_P95_MS and missed == 0
                    and (false_interrupt is None or false_interrupt <= FALSE_INTERRUPT_MAX)}


def build_result(turns_: list[dict], *, mode: str, version: str, git_sha: str, machine: dict, config: dict,
                 at: dt.datetime | None = None) -> dict:
    return {"schema": SCHEMA, "version": version, "git_sha": git_sha,
            "at": (at or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds"), "mode": mode,
            "machine": dict(machine), "config": dict(config), "turns": turns_, "summary": summarize(turns_)}


def validate(result) -> list[str]:
    """What is wrong with a results document against the schema ([] = fine). Field names are exact: the
    /diag/voice page and `kairo diag` read them."""
    problems: list[str] = []

    def keys(obj, expected, where) -> bool:
        """True when obj has exactly the expected keys (else the problem is noted and its contents not looked at)."""
        if not isinstance(obj, dict):
            problems.append(f"{where}: not an object")
            return False
        if set(obj) != expected:
            problems.append(f"{where}: keys {sorted(set(obj) ^ expected)} differ from the schema")
            return False
        return True

    def num(v, where, nullable=False):
        if v is None and nullable:
            return
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            problems.append(f"{where}: {v!r} is not a number" + (" or null" if nullable else ""))

    if not keys(result, TOP_KEYS, "result"):
        return problems
    if result["schema"] != SCHEMA:
        problems.append(f"schema {result['schema']!r}")
    if result["mode"] not in MODES:
        problems.append(f"mode {result['mode']!r}")
    for k in ("version", "git_sha"):
        if not isinstance(result[k], str) or not result[k]:
            problems.append(f"{k}: a non-empty string")
    try:
        dt.datetime.fromisoformat(result["at"])
    except (TypeError, ValueError):
        problems.append("at: not ISO-8601")
    if keys(result["machine"], MACHINE_KEYS, "machine"):
        m = result["machine"]
        if m["gpu"] is not None and not isinstance(m["gpu"], str):
            problems.append("machine.gpu: a string or null")
        if not isinstance(m["cpu"], str):
            problems.append("machine.cpu: a string")
        num(m["vram_gb"], "machine.vram_gb", True)
        num(m["ram_gb"], "machine.ram_gb", True)
    if keys(result["config"], CONFIG_KEYS, "config"):
        c = result["config"]
        num(c["vad_stop_s"], "config.vad_stop_s")
        for k in CONFIG_KEYS - {"vad_stop_s", "llm_model"}:
            if not isinstance(c[k], str):
                problems.append(f"config.{k}: a string")
        if c["llm_model"] is not None and not isinstance(c["llm_model"], str):
            problems.append("config.llm_model: a string or null")
    if not isinstance(result["turns"], list):
        problems.append("turns: not a list")
    else:
        for i, t in enumerate(result["turns"]):
            where = f"turns[{i}]"
            if not keys(t, TURN_KEYS, where):
                continue
            if t["lang"] not in ("en", "de") or not isinstance(t["clip"], str) or not isinstance(t["kind"], str):
                problems.append(f"{where}: clip/lang/kind")
            if keys(t["stages_ms"], set(STAGES), f"{where}.stages_ms"):
                for s in STAGES:
                    num(t["stages_ms"][s], f"{where}.stages_ms.{s}", True)
            num(t["total_ms"], f"{where}.total_ms", True)
            for k in ("interrupted", "speaker_accept"):
                if t[k] is not None and not isinstance(t[k], bool):
                    problems.append(f"{where}.{k}: a boolean or null")
            if not isinstance(t["missed"], bool):
                problems.append(f"{where}.missed: a boolean")
    if keys(result["summary"], SUMMARY_KEYS, "summary"):
        s = result["summary"]
        if not isinstance(s["n"], int) or isinstance(s["n"], bool):
            problems.append("summary.n: an integer")
        for k in ("p50_ms", "p90_ms", "p95_ms", "false_interrupt_rate", "speaker_false_accept_rate"):
            num(s[k], f"summary.{k}", True)
        num(s["missed_turn_rate"], "summary.missed_turn_rate")
        num(s["target_p95_ms"], "summary.target_p95_ms")
        if not isinstance(s["pass"], bool):
            problems.append("summary.pass: a boolean")
        if not isinstance(s["by_stage"], dict) or any(
                k not in STAGES or not isinstance(v, dict) or set(v) != {"p50", "p95"} for k, v in s["by_stage"].items()):
            problems.append("summary.by_stage: {stage: {p50, p95}}")
    return problems


# ---------------------------------------------------------------- the results folder

def save(result: dict, out_dir: Path) -> Path:
    """<out_dir>/voice-<UTC time>-<mode>.json (a name that sorts by time)."""
    problems = validate(result)
    if problems:
        raise BenchError("not saving a results document that breaks the schema: " + "; ".join(problems[:3]))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.fromisoformat(result["at"]).astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{PREFIX}{stamp}-{result['mode']}.json"
    n = 1
    while path.exists():                                # two runs in one second
        n += 1
        path = out_dir / f"{PREFIX}{stamp}-{result['mode']}-{n}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def latest(results_dir: Path, mode: str | None = None) -> dict | None:
    """The newest valid results document in the folder (of this mode, if given), or None."""
    try:
        files = sorted(Path(results_dir).glob(f"{PREFIX}*.json"), key=lambda p: p.stem, reverse=True)
    except OSError:
        return None
    for f in files:
        try:
            doc = json.loads(f.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not validate(doc) and (mode is None or doc["mode"] == mode):
            return doc
    return None


def format_table(result: dict) -> str:
    """The summary as text for the terminal."""
    s, cfg, mach = result["summary"], result["config"], result["machine"]
    gpu = f"{mach['gpu']} {mach['vram_gb']} GB" if mach["gpu"] else "no GPU"
    lines = [f"Voice latency, {result['mode']} run at {result['at']} (Kairo {result['version']}, {result['git_sha']})",
             f"  machine: {mach['cpu']}; {gpu}; RAM {mach['ram_gb']} GB",
             f"  voice: STT {cfg['stt_model']} ({cfg['stt_compute_type']}), LLM {cfg['llm_model']}, TTS {cfg['tts_en']} / {cfg['tts_de']}, "
             f"VAD pause {cfg['vad_stop_s']} s, turn {cfg['turn_analyzer']}", "",
             f"  {'stage':18}{'p50 ms':>8}{'p95 ms':>8}"]
    for name in STAGES:
        v = s["by_stage"].get(name)
        lines.append(f"  {name:18}{(v['p50'] if v else '-'):>8}{(v['p95'] if v else '-'):>8}")
    lines.append(f"  {'TOTAL (n=' + str(s['n']) + ')':18}{_fmt(s['p50_ms']):>8}{_fmt(s['p95_ms']):>8}   p90 {_fmt(s['p90_ms'])}")
    lines += ["", f"  p95 target {s['target_p95_ms']} ms; missed turns {_pc(s['missed_turn_rate'])}; "
                  f"false interrupts {_pc(s['false_interrupt_rate'])}; speaker false accepts {_pc(s['speaker_false_accept_rate'])}",
              f"  RESULT: {'PASS' if s['pass'] else 'FAIL'}"]
    return "\n".join(lines)


def _fmt(v) -> str:
    return "-" if v is None else str(v)


def _pc(v) -> str:
    return "n/a" if v is None else f"{v * 100:.0f}%"


# ---------------------------------------------------------------- Kairo's own address

CALL_BLOCKS = {"not_installed": "the voice packages (Pipecat, aiortc) are not installed on that Kairo",
               "voice_missing": "speech recognition is not installed there (run Update Kairo)",
               "no_tailnet": "Tailscale is down on that PC: a call over WebRTC needs its tailnet address"}
CALL_FRESH_H = 3                                         # an "answered" call that never ended counts for 3 hours (voice/calls.py)
MOCK_CLIPS = 12                                          # a mock run is a quick check: two clips of each kind


def check_url(url: str) -> str:
    """The base address to call: Kairo on this machine (http://127.0.0.1:<port>), or Kairo's own tailnet address
    (https://<name>.ts.net). Nothing else: the harness never calls a host that isn't Kairo's."""
    bad = BenchError(f"{url!r} is not Kairo's own address: use http://127.0.0.1:<port> on the PC itself, or its https://<name>.ts.net address")
    parts = urlsplit(url.strip())
    try:
        port = parts.port
    except ValueError:
        raise bad from None
    host = (parts.hostname or "").lower()
    if parts.username or parts.password or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise bad
    if not ((parts.scheme == "http" and host in ("127.0.0.1", "localhost", "::1"))
            or (parts.scheme == "https" and port in (None, 443) and outbound.tailnet_url_ok(f"https://{host}"))):
        raise bad
    return f"{parts.scheme}://{parts.netloc}"


def is_local(base: str) -> bool:
    return (urlsplit(base).hostname or "") in ("127.0.0.1", "localhost", "::1")


class Kairo:
    """Kairo's HTTP API, called the way the owner's phone would. On the PC itself (127.0.0.1) the caller is the
    owner already (main.is_local_request: loopback, loopback Host, nothing forwarded): no token, no setting, no
    new door. From another machine only through `tailscale serve`, as the owner's signed-in Tailscale account."""

    def __init__(self, base: str, transport=None) -> None:
        self.base = base
        self.http = httpx.AsyncClient(base_url=base, timeout=20, transport=transport)

    async def close(self) -> None:
        await self.http.aclose()

    async def request(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = await self.http.request(method, path, **kw)
        except httpx.TransportError as exc:
            raise BenchError(f"Kairo isn't answering at {self.base} ({type(exc).__name__}): start it, or give --url") from exc
        if r.status_code == 403:
            raise BenchError("Kairo answers only its owner: run the harness on the PC itself with --url http://127.0.0.1:<port> "
                             "(this machine's own address needs no token)")
        return r

    async def get(self, path: str):
        r = await self.request("GET", path)
        try:
            return r.json() if r.status_code == 200 else None
        except ValueError:
            return None

    async def offer(self, sdp: str, kind: str) -> dict:
        """The call: the fake phone's offer in, Kairo's answer out. path "call": the line with the speaker check."""
        r = await self.request("POST", "/api/rtc/offer", json={"sdp": sdp, "type": kind, "path": "call"})
        if r.status_code == 409:
            try:
                why = r.json().get("detail")
            except (ValueError, AttributeError):
                why = None
            raise BenchError("Kairo can't take a call over WebRTC now: " + CALL_BLOCKS.get(why, str(why)))
        if r.status_code != 200:
            raise BenchError(f"Kairo refused the call: HTTP {r.status_code} {r.text[:200]}")
        return r.json()


async def preflight(kairo: Kairo, *, force: bool = False, now: dt.datetime | None = None) -> dict:
    """Is Kairo there, can it take a WebRTC call, is nobody on a call (a new call replaces the old one: the
    owner's call would be hung up). {"health", "devices", "speaker"} for the report."""
    health = await kairo.get("/health")
    if not isinstance(health, dict):
        raise BenchError(f"{kairo.base}/health did not answer: is that Kairo, and is it running?")
    rtc = await kairo.get("/api/rtc") or {}
    if not rtc.get("available"):
        raise BenchError(CALL_BLOCKS.get(rtc.get("reason"), f"Kairo can't take a call over WebRTC: {rtc.get('reason') or 'unknown'}"))
    since = (now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(hours=CALL_FRESH_H)
    live = []
    for c in await kairo.get("/api/voice/calls") or []:
        try:
            fresh = dt.datetime.fromisoformat(str(c.get("created_at"))) > since
        except (ValueError, AttributeError):
            fresh = False
        if fresh and c.get("status") in ("ringing", "answered"):
            live.append(c)
    if live and not force:
        raise BenchError("a call looks to be under way on Kairo: the harness's call would replace it and hang it up. Hang up first, "
                         "or pass --force if that call is stale")
    return {"health": health, "devices": await kairo.get("/api/voice/devices") or {},
            "speaker": await kairo.get("/api/voice/speaker") or {}}


# ---------------------------------------------------------------- what the run is made on

def _ram_gb() -> float | None:
    try:
        if sys.platform == "win32":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("avail", ctypes.c_ulonglong), ("page_total", ctypes.c_ulonglong), ("page_avail", ctypes.c_ulonglong),
                            ("virt_total", ctypes.c_ulonglong), ("virt_avail", ctypes.c_ulonglong), ("ext", ctypes.c_ulonglong)]
            m = MemoryStatus()
            m.length = ctypes.sizeof(MemoryStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return round(m.total / 2 ** 30, 1) or None
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2 ** 30, 1)
    except (AttributeError, ValueError, OSError):
        return None


def _cpu_name() -> str:
    name = ""
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                name = winreg.QueryValueEx(k, "ProcessorNameString")[0]
        else:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace").splitlines():
                if line.lower().startswith("model name"):
                    name = line.split(":", 1)[1]
                    break
    except (OSError, ImportError, IndexError):
        pass
    return " ".join((name or platform.processor() or platform.machine() or "unknown").split())


def machine_info(card: dict | None = None, *, local: bool = True) -> dict:
    """{gpu, vram_gb, ram_gb, cpu} of this machine (nvidia-smi for the card). card: the card as a Kairo on another
    machine reports it (gpu.summary(), in /api/voice/devices); local=False: the rest isn't known."""
    card = gpu.summary() if card is None and local else card
    return {"gpu": (card or {}).get("name") or None,
            "vram_gb": round(card["total_mb"] / 1024, 1) if card and card.get("total_mb") else None,
            "ram_gb": _ram_gb() if local else None, "cpu": _cpu_name() if local else "another machine (not reported)"}


TURN_ANALYZER = f"smart-turn-v3 (pipecat), pause at most {TURN_MAX_SILENCE_S} s"


def config_info(devices: dict | None) -> dict:
    """The voice setup of the Kairo that was measured, from its own /api/voice/devices (where each part runs)."""
    comps = (devices or {}).get("components") or {}
    whisper, kokoro, piper, vm = (comps.get(k) or {} for k in ("whisper", "kokoro", "piper", "voice_model"))
    return {"vad_stop_s": VAD_STOP_S, "turn_analyzer": TURN_ANALYZER,
            "stt_model": str(whisper.get("model") or "unknown"),
            "stt_compute_type": {"cuda": "float16", "cpu": "int8"}.get(whisper.get("device"), "unknown"),   # voice.Voice._make_stt
            "llm_model": str(vm["model"]) if vm.get("loaded") and vm.get("model") else None,
            "tts_en": f"kokoro {kokoro.get('model') or KOKORO_MODEL} ({KOKORO_VOICE}, {kokoro.get('device') or 'not loaded'})",
            "tts_de": f"piper {piper.get('model') or PIPER_DE} ({piper.get('device') or 'cpu'})"}


def mock_config(delays) -> dict:
    def waits(s: float) -> str:
        return f"mock, {round(s * 1000)} ms"
    return {"vad_stop_s": VAD_STOP_S, "turn_analyzer": TURN_ANALYZER, "stt_model": waits(delays.stt_s), "stt_compute_type": "none",
            "llm_model": None, "tts_en": waits(delays.tts_s), "tts_de": waits(delays.tts_s)}


def version_info(server_version: str | None = None) -> tuple[str, str]:
    """(version, git sha) of what was measured: the running Kairo's version (yyyy.MM.dd.HHmm-<sha>), else this checkout's."""
    version = server_version or versions.running() or "dev"
    m = re.search(r"-([0-9a-f]{7,40})(?:-dirty)?$", version)
    sha = m.group(1) if m else None
    if not sha:
        try:
            sha = json.loads((REPO_ROOT / "release.json").read_text(encoding="utf-8-sig")).get("git_sha")
        except (OSError, ValueError, AttributeError):
            sha = None
    if not sha and shutil.which("git"):
        try:
            out = subprocess.run(["git", "rev-parse", "--short=7", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=5)
            sha = out.stdout.strip() if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            sha = None
    return version, sha or "unknown"


# ---------------------------------------------------------------- one run

async def execute(mock: bool, url: str | None, out_dir: Path, *, clips=None, data_dir: Path | None = None, warmup: int = 1,
                  notes: list[str] | None = None, force: bool = False, delays=None, transport=None, before=None) -> tuple[dict, Path]:
    """Run the corpus against the mock call (mock=True) or against Kairo at `url` (default: this PC, port $KAIRO_PORT or
    8000), save the result in out_dir and return (result, its file). clips: a count (a balanced few), a list of
    bench_corpus.Clip, or None (all; a mock run: MOCK_CLIPS). notes: gets what a person should know about the run.
    transport, before: for tests (an httpx transport for Kairo; a hook called before each clip)."""
    notes = notes if notes is not None else []
    if not voicertc.available():
        raise BenchError("the call packages (Pipecat, aiortc) are not installed here: pip install -r server/requirements-voice.txt")
    from . import bench_phone as bp
    chosen = list(clips) if isinstance(clips, (list, tuple)) else bc.select(bc.load_manifest(), clips if clips is not None
                                                                             else (MOCK_CLIPS if mock else None))
    if not chosen:
        raise BenchError("no clips to play")
    async with contextlib.AsyncExitStack() as stack:
        if mock:
            world = bp.MockWorld(delays=delays or bp.MockDelays())
            pipeline = bp.MockPipeline(world)
            stack.push_async_callback(pipeline.stop)
            stack.enter_context(bp.ice_rules(loopback=True))
            rendered, signal, check, still, before = bp.mock_clips(chosen), pipeline.signal, True, 0.6, world.before
            machine, config, (version, sha) = machine_info(), mock_config(world.delays), version_info()
        else:
            base = check_url(url or f"http://127.0.0.1:{get_settings().port}")
            kairo = Kairo(base, transport=transport)
            stack.push_async_callback(kairo.close)
            data = Path(data_dir) if data_dir else get_settings().data_dir
            rendered = [bc.load(data, c) for c in chosen]
            absent = [c.id for c, r in zip(chosen, rendered) if r is None]
            if absent:
                raise BenchError(f"{len(absent)} of {len(chosen)} clips are not rendered yet ({', '.join(absent[:4])}…): run  "
                                 f"python -m app.bench_corpus render   (your own recordings go to {bc.own_dir(data)})")
            info = await preflight(kairo, force=force)
            on = bool((info["speaker"] or {}).get("check"))
            if not on:
                notes.append("the speaker check is off or has no voiceprint, so other_speaker clips can't be labelled "
                             f"({(info['speaker'] or {}).get('why_off') or 'no information'})")
            elif not any(r.source == "own" for r in rendered if r.clip.kind in bc.ANSWERED):
                notes.append("the speaker check is on and none of the questions is your own recording: Kairo refuses these synthetic "
                             f"voices as not you (not counted as missed). Put recordings in {bc.own_dir(data)}")
            stack.enter_context(bp.ice_rules(loopback=False))
            signal, check, still = kairo.offer, on or None, bp.STILL_S
            local = is_local(base)
            machine = machine_info(None if local else (info["devices"] or {}).get("gpu"), local=local)
            config = config_info(info["devices"])
            version, sha = version_info(str(info["health"].get("version") or ""))
        phone = bp.Phone(still_s=still)
        stack.push_async_callback(phone.close)
        await phone.connect(signal)
        records = await bp.Bench(phone, rendered, speaker_check=check, warmup=warmup, before=before, notes=notes).run()
    result = build_result(records, mode="mock" if mock else "live", version=version, git_sha=sha, machine=machine, config=config)
    return result, save(result, out_dir)


def _sync(coro):
    """Run the coroutine to its end from sync code, also when this thread already has an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: dict = {}

    def work() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:      # handed back to the caller's thread
            box["error"] = exc
    t = threading.Thread(target=work, name="bench-voice", daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def run(mock: bool, url: str | None, out_dir: Path, **kw) -> dict:
    """The harness as a function (for `kairo diag`): run, save to out_dir, return the results document (the schema
    validate() checks). mock=True: the in-process mock call, no models or Kairo needed (about a minute). Else Kairo at
    url (None: this PC). Keywords: clips, data_dir, warmup, notes, force, delays. Raises BenchError."""
    return _sync(execute(mock, url, Path(out_dir), **kw))[0]


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m app.bench_voice", description=__doc__.split("\n\n")[0])
    ap.add_argument("--mock", action="store_true", help="the same client and pipeline with fake models of known speed (no Kairo needed)")
    ap.add_argument("--url", default=None, help="Kairo's address (default http://127.0.0.1:$KAIRO_PORT); only this PC or its own .ts.net")
    ap.add_argument("--clips", type=int, default=None, metavar="N", help=f"a balanced N clips (default: all; --mock: {MOCK_CLIPS})")
    ap.add_argument("--out", type=Path, default=None, help="results folder (default <data>/bench/results)")
    ap.add_argument("--data-dir", type=Path, default=None, help="Kairo's data folder (default KAIRO_DATA_DIR / devdata)")
    ap.add_argument("--warmup", type=int, default=1, help="turns played first and not measured (default 1)")
    ap.add_argument("--force", action="store_true", help="call even if a call looks to be under way (it will be replaced)")
    ap.add_argument("--strict", action="store_true", help="exit 1 when the run fails its targets")
    args = ap.parse_args(argv)
    data_dir = args.data_dir or get_settings().data_dir
    notes: list[str] = []
    try:
        result, path = _sync(execute(args.mock, args.url, args.out or bc.results_dir(data_dir), clips=args.clips, data_dir=data_dir,
                                     warmup=args.warmup, notes=notes, force=args.force))
    except (BenchError, bc.CorpusError) as exc:
        print(f"voice bench: {exc}", file=sys.stderr)
        return 1
    print(format_table(result))
    for n in notes:
        print(f"  note: {n}")
    print(f"\nresults: {path}")
    return 1 if args.strict and not result["summary"]["pass"] else 0


if __name__ == "__main__":
    sys.exit(main())
