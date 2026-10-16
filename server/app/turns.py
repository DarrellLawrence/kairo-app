"""Every voice and chat turn, step by step (owner's request 2026-10-15).

The phone reports when each step happened, in ms since the end of the owner's speech (or Send):
endpoint (recording stopped), transcribed, sent, first_words, first_sentence, audio_ready, playing.
The PC adds what only it knows (speech to text, waiting for the brain). turns.steps() turns both
into step durations. More -> System -> Voice speed shows p50/p95 per step for this version and the
one before (before/after) and the last turns with their model and whether the audio played.

Voice V3 (owner's request 2026-10-16): a turn over WebRTC has the PC's own clock from the end of speech
it heard (turn decided, first word, first audio sent) and the phone's (Kairo heard); what's left between
the PC sending audio and the phone playing it is the network and the phone's playout. Every spoken turn
over 1.0 s is logged with its slowest step and listed in Voice speed.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

from sqlmodel import Session, delete, select

from .db import VoiceTurn, utcnow
from .latency import pct

log = logging.getLogger("kairo.turns")

KEEP_DAYS = 30
SLOW_MS = 1000             # owner's request 2026-10-16: every spoken turn over this is logged with its slowest step
MAX_MS = 10 * 60 * 1000
PATHS = {"call": "Call", "chat_voice": "Chat, spoken", "chat_text": "Chat, typed",
         "call_live": "Call, realtime", "chat_live": "Voice button, realtime"}
OUTCOMES = {"played", "text", "blocked", "failed", "interrupted", "no_speech"}
MARKS = ("endpoint", "transcribed", "sent", "first_words", "first_sentence", "audio_ready", "playing")
STEPS = {   # step -> title, in the order they happen
    "endpoint_ms": "End of speech noticed",
    "upload_ms": "Upload",
    "stt_ms": "Speech to text (PC)",
    "queue_ms": "Waiting for the brain",
    "llm_ms": "Voice model: first word (PC)",
    "first_words_ms": "Model: first words",
    "tool_ms": "Claude working (ask_kairo)",
    "first_sentence_ms": "First sentence written",
    "tts_ms": "Its voice made and fetched",
    "play_start_ms": "Playback starts",
    "net_ms": "Network + playout on the phone (WebRTC)",
    "rtt_ms": "Network round trip (WebRTC, not added up)",
    "total_ms": "Total: you stop -> Kairo speaks (typed: first words)",
}
RTC_TITLES = {"endpoint_ms": "Turn decided on the PC (pause + Smart Turn)", "stt_ms": "Speech to text (PC, inside the pause)",
              "llm_ms": "First word (voice model or Claude)", "tts_ms": "First audio made (or from the cache)"}
NOT_A_STEP = ("total_ms", "rtt_ms")
SERVER_KEYS = ("stt_ms", "queue_ms", "llm_ms", "tool_ms", "turn_at_ms", "first_token_at_ms", "first_audio_at_ms", "rtt_ms")
# owner's request 2026-10-16 (Voice V3): end of speech -> first audio, p95 1.0 s, p50 0.65 s (acceptance 3, 4, 15, 39)
TARGETS = {"call": {"p50": 650, "p95": 1000}, "chat_voice": {"p50": 650, "p95": 1000}, "chat_text": {"p50": 1500, "p95": None},
           # realtime (owner's request 2026-10-15): end of speech -> first audio on the iPhone
           "call_live": {"p50": 800, "p95": 1500}, "chat_live": {"p50": 800, "p95": 1500}}


class TurnError(ValueError):
    pass


def _ms(v) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= MAX_MS:
        raise TurnError("times are ms between 0 and 10 minutes")
    return int(v)


def steps(marks: dict, server: dict, path: str = "chat_text") -> dict[str, int]:
    """Step durations from the phone's cumulative marks and the PC's own numbers. A spoken turn's
    total is to its first audio: one that never played has none (it counts as a failure)."""
    m = {k: _ms(v) for k, v in (marks or {}).items() if k in MARKS and v is not None}
    srv = {k: _ms(v) for k, v in (server or {}).items() if k in SERVER_KEYS and v is not None}
    out: dict[str, int] = {}
    if "first_audio_at_ms" in srv:             # over WebRTC: the PC's clock, then the network to the phone
        return _rtc_steps(m, srv, path)

    def gap(a, b):
        return max(0, m[b] - m[a]) if a in m and b in m else None

    if "endpoint" in m:
        out["endpoint_ms"] = m["endpoint"]
    if (g := gap("endpoint", "transcribed")) is not None:
        stt = min(srv.get("stt_ms", 0), g)
        out["upload_ms"], out["stt_ms"] = g - stt, stt
    if "first_words" in m:
        queue = srv.get("queue_ms", 0)
        start = m.get("sent", m.get("transcribed", 0))
        out["queue_ms"] = queue
        out["first_words_ms"] = max(0, m["first_words"] - start - queue)
    for key in ("llm_ms", "tool_ms"):      # the local voice model's own numbers (PC side)
        if key in srv:
            out[key] = srv[key]
    for key, a, b in (("first_sentence_ms", "first_words", "first_sentence"), ("tts_ms", "first_sentence", "audio_ready"),
                      ("play_start_ms", "audio_ready", "playing")):
        if (g := gap(a, b)) is not None:
            out[key] = g
    total = m.get("first_words") if path == "chat_text" else m.get("playing")
    if total is not None:
        out["total_ms"] = total
    return out


def _rtc_steps(m: dict, srv: dict, path: str) -> dict[str, int]:
    out: dict[str, int] = {}
    turn, token, audio = srv.get("turn_at_ms"), srv.get("first_token_at_ms"), srv["first_audio_at_ms"]
    if turn is not None:
        out["endpoint_ms"] = turn
    if "stt_ms" in srv:
        out["stt_ms"] = srv["stt_ms"]
    if "tool_ms" in srv:
        out["tool_ms"] = srv["tool_ms"]
    if token is not None and turn is not None:
        out["llm_ms"] = max(0, token - turn)
    if token is not None:
        out["tts_ms"] = max(0, audio - token)
    if "playing" in m:
        out["net_ms"] = max(0, m["playing"] - audio)
        out["total_ms"] = m["playing"]
    if "rtt_ms" in srv:
        out["rtt_ms"] = srv["rtt_ms"]
    return out


def slowest(steps_: dict[str, int]) -> str | None:
    parts = {k: v for k, v in steps_.items() if k not in NOT_A_STEP and k in STEPS}
    if "net_ms" in parts:                       # over WebRTC speech to text runs inside the pause
        parts.pop("stt_ms", None)
    return max(parts, key=parts.get) if parts else None


def record(s: Session, data: dict, *, version: str) -> VoiceTurn:
    if not isinstance(data, dict):
        raise TurnError("not a turn")
    path, outcome = data.get("path"), data.get("outcome")
    if path not in PATHS:
        raise TurnError("unknown path")
    if outcome not in OUTCOMES:
        raise TurnError("unknown outcome")
    model = data.get("model")
    if model is not None and (not isinstance(model, str) or len(model) > 40):
        raise TurnError("model is a short name")
    st = steps(data.get("marks") or {}, data.get("server") or {}, path)
    device = data.get("device") if isinstance(data.get("device"), dict) else {}
    device = {k: (v if isinstance(v, (bool, int, float)) else str(v)[:40]) for k, v in list(device.items())[:8]}
    row = VoiceTurn(version=str(data.get("version") or version)[:40], path=path, model=model, total_ms=st.get("total_ms"),
                    steps_json=json.dumps(st), outcome=outcome, reason=(str(data["reason"])[:200] if data.get("reason") else None),
                    device_json=json.dumps(device))
    s.add(row)
    s.commit()
    total = st.get("total_ms")
    if path in ("call", "chat_voice") and total is not None and total > SLOW_MS:
        worst = slowest(st)
        log.warning("slow turn: %d ms (%s), slowest step %s %s ms, line %s, network %s", total, path, worst,
                    st.get(worst) if worst else "?", device.get("line", "ws"), device.get("net", "?"))
    return row


def prune(s: Session, now: dt.datetime | None = None) -> int:
    cutoff = (now or utcnow()) - dt.timedelta(days=KEEP_DAYS)
    n = len(s.exec(select(VoiceTurn.id).where(VoiceTurn.ts < cutoff)).all())
    s.exec(delete(VoiceTurn).where(VoiceTurn.ts < cutoff))
    s.commit()
    return n


def _aware(t: dt.datetime) -> dt.datetime:
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def summary(s: Session, days: int = 7, now: dt.datetime | None = None, versions: int = 2) -> dict:
    """p50/p95 per step and path for the newest `versions` app versions with turns (before/after),
    the failures by reason, and the last 20 turns."""
    since = (now or utcnow()) - dt.timedelta(days=days)
    rows = s.exec(select(VoiceTurn).where(VoiceTurn.ts >= since).order_by(VoiceTurn.ts.desc())).all()
    order: list[str] = []
    for r in rows:
        if r.version not in order:
            order.append(r.version)
    out_versions = []
    for v in order[:versions]:
        paths = {}
        for p in PATHS:
            mine = [r for r in rows if r.version == v and r.path == p]
            if not mine:
                continue
            per_step: dict[str, list[int]] = {}
            for r in mine:
                for k, ms in json.loads(r.steps_json or "{}").items():
                    per_step.setdefault(k, []).append(ms)
            failures: dict[str, int] = {}
            for r in mine:
                if r.outcome not in ("played", "text"):
                    key = f"{r.outcome}: {r.reason}" if r.reason else r.outcome
                    failures[key] = failures.get(key, 0) + 1
            paths[p] = {"title": PATHS[p], "n": len(mine), "played": sum(r.outcome in ("played", "text") for r in mine),
                        "steps": {k: {"title": STEPS[k], "p50": pct(per_step[k], .5), "p95": pct(per_step[k], .95)}
                                  for k in STEPS if k in per_step},
                        "failures": failures}
        out_versions.append({"version": v, "since": _aware(min(r.ts for r in rows if r.version == v)).isoformat(),
                             "paths": paths})
    def line_of(r) -> dict:
        try:
            d = json.loads(r.device_json or "{}")
        except ValueError:
            d = {}
        return {"line": d.get("line") or "ws", "net": d.get("net"), "via": d.get("via")}
    recent = [{"ts": _aware(r.ts).isoformat(), "version": r.version, "path": r.path, "model": r.model,
               "total_ms": r.total_ms, "outcome": r.outcome, "reason": r.reason, "steps": json.loads(r.steps_json or "{}"),
               **line_of(r)}
              for r in rows[:20]]
    slow = []
    for r in rows:
        if r.path in ("call", "chat_voice") and r.total_ms is not None and r.total_ms > SLOW_MS:
            st = json.loads(r.steps_json or "{}")
            worst = slowest(st)
            slow.append({"ts": _aware(r.ts).isoformat(), "total_ms": r.total_ms, "slowest": worst,
                         "slowest_title": STEPS.get(worst) if worst else None, "slowest_ms": st.get(worst) if worst else None,
                         **line_of(r)})
        if len(slow) >= 20:
            break
    causes: dict[str, int] = {}
    for x in slow:
        if x["slowest"]:
            causes[x["slowest"]] = causes.get(x["slowest"], 0) + 1
    net = {}
    for r in rows:
        li = line_of(r)
        if li["line"] == "webrtc":
            st = json.loads(r.steps_json or "{}")
            k = li["net"] or "unknown"
            net.setdefault(k, []).append(st.get("rtt_ms"))
    network = {k: {"turns": len(v), "rtt_p50": pct([x for x in v if x is not None], .5)} for k, v in net.items()}
    return {"days": days, "versions": out_versions, "recent": recent, "targets": TARGETS, "titles": STEPS,
            "rtc_titles": RTC_TITLES, "slow": slow, "slow_causes": dict(sorted(causes.items(), key=lambda kv: -kv[1])),
            "network": network}
