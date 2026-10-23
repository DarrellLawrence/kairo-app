"""The voice latency harness (owner's spec): how long after you stop talking does Kairo's voice start, step by
step, on the real call pipeline.

A fake phone (aiortc) calls Kairo over WebRTC like the iPhone does: it posts an SDP offer to /api/rtc/offer,
plays each clip of the corpus (app/bench_corpus.py, bench/voice/corpus.json) at real time, then silence, and
records the reply audio. Per turn: total_ms = the clip's true end of speech -> the first reply frame that is
audible at the client (RMS >= 0.01 like the phone's "playing" mark). The stages come from the server's own
numbers for that turn (the data channel's "timing" event, the same `server` dict the phone sends to
app/turns.py, which turns.steps() turns into steps), joined by turn number; what is left over is "network".

    python -m app.bench_voice [--mock] [--url http://127.0.0.1:8000] [--clips N] [--out DIR]

Rates: false_interrupt_rate (a backchannel or noise clip is played while Kairo is talking: did it stop?),
missed_turn_rate (a real utterance got no reply within 5 s), speaker_false_accept_rate (an other_speaker
clip answered although the speaker check is on: null when the check is off or has no voiceprint, as the
check's per-turn label is the only evidence). Results: one JSON per run in <data>/bench/results/ (schema
below), with pass = p95 <= 1000 ms and no missed turn and false interrupts <= 5 %.

--mock runs the same client against the same Pipecat pipeline (Silero VAD, Smart Turn) in this process, with
fake speech to text, voice model and voice that have fixed, known delays, so the harness itself can be tested
without models, GPU or a running Kairo.

Measured at 20 ms frames: the speech end and the reply onset are good to about 20 ms (Windows timers add up
to 16 ms). The network stage also holds the difference between the VAD's idea of the end of speech and the
true one, and it is never negative (turns.steps clamps it).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import turns
from .bench_corpus import ANSWERED, PROBES
from .latency import pct

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
