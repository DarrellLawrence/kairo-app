"""Every voice and chat turn, step by step (owner's request 2026-10-15).

The phone reports when each step happened, in ms since the end of the owner's speech (or Send):
endpoint (recording stopped), transcribed, sent, first_words, first_sentence, audio_ready, playing.
The PC adds what only it knows (speech to text, waiting for the brain). turns.steps() turns both
into step durations. More -> System -> Voice speed shows p50/p95 per step for this version and the
one before (before/after) and the last turns with their model and whether the audio played.
"""

from __future__ import annotations

import datetime as dt
import json

from sqlmodel import Session, delete, select

from .db import VoiceTurn, utcnow
from .latency import pct

KEEP_DAYS = 30
MAX_MS = 10 * 60 * 1000
PATHS = {"call": "Call", "chat_voice": "Chat, spoken", "chat_text": "Chat, typed"}
OUTCOMES = {"played", "text", "blocked", "failed", "interrupted", "no_speech"}
MARKS = ("endpoint", "transcribed", "sent", "first_words", "first_sentence", "audio_ready", "playing")
STEPS = {   # step -> title, in the order they happen
    "endpoint_ms": "End of speech noticed",
    "upload_ms": "Upload",
    "stt_ms": "Speech to text (PC)",
    "queue_ms": "Waiting for the brain",
    "first_words_ms": "Model: first words",
    "first_sentence_ms": "First sentence written",
    "tts_ms": "Its voice made and fetched",
    "play_start_ms": "Playback starts",
    "total_ms": "Total: you stop -> Kairo speaks (typed: first words)",
}
TARGETS = {"call": {"p50": 1500, "p95": 3000}, "chat_voice": {"p50": 1500, "p95": 3000}, "chat_text": {"p50": 1500, "p95": None}}


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
    srv = {k: _ms(v) for k, v in (server or {}).items() if k in ("stt_ms", "queue_ms") and v is not None}
    out: dict[str, int] = {}

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
    for key, a, b in (("first_sentence_ms", "first_words", "first_sentence"), ("tts_ms", "first_sentence", "audio_ready"),
                      ("play_start_ms", "audio_ready", "playing")):
        if (g := gap(a, b)) is not None:
            out[key] = g
    total = m.get("first_words") if path == "chat_text" else m.get("playing")
    if total is not None:
        out["total_ms"] = total
    return out


def record(s: Session, data: dict, *, version: str) -> VoiceTurn:
    if not isinstance(data, dict):
        raise TurnError("not a turn")
    path, outcome = data.get("path"), data.get("outcome")
    if path not in PATHS:
        raise TurnError("unknown path")
    if outcome not in OUTCOMES:
        raise TurnError("unknown outcome")
    model = data.get("model")
    if model is not None and (not isinstance(model, str) or len(model) > 20):
        raise TurnError("model is a short name")
    st = steps(data.get("marks") or {}, data.get("server") or {}, path)
    device = data.get("device") if isinstance(data.get("device"), dict) else {}
    device = {k: (v if isinstance(v, (bool, int, float)) else str(v)[:40]) for k, v in list(device.items())[:8]}
    row = VoiceTurn(version=str(data.get("version") or version)[:40], path=path, model=model, total_ms=st.get("total_ms"),
                    steps_json=json.dumps(st), outcome=outcome, reason=(str(data["reason"])[:200] if data.get("reason") else None),
                    device_json=json.dumps(device))
    s.add(row)
    s.commit()
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
    recent = [{"ts": _aware(r.ts).isoformat(), "version": r.version, "path": r.path, "model": r.model,
               "total_ms": r.total_ms, "outcome": r.outcome, "reason": r.reason, "steps": json.loads(r.steps_json or "{}")}
              for r in rows[:20]]
    return {"days": days, "versions": out_versions, "recent": recent, "targets": TARGETS, "titles": STEPS}
