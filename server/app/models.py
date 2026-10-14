"""Which Claude model does which job (v7): the cheapest one that does it well.

Haiku: triage, classification, summaries, reminders, simple replies.
Sonnet: writing, drafts, documents, self-check loops.
Opus: big planning and weekly-or-longer reviews only.
schedule.yaml can override any purpose under `models:`.
"""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session, func, select

from .db import ModelRun, utcnow

ALIASES = ("haiku", "sonnet", "opus")
DEFAULTS = {
    "chat": "sonnet", "call": "haiku", "voice_note": "sonnet",
    "triage": "haiku", "capture": "haiku", "letters": "haiku", "brief": "haiku", "reminder": "haiku",
    "proactive": "haiku", "summary": "haiku", "promises": "haiku",
    "draft": "sonnet", "document": "sonnet", "gauntlet": "sonnet", "study": "sonnet", "lerndoku": "sonnet",
    "voice_check": "sonnet",
    "plan": "opus", "review_big": "opus",
}
JOB_PURPOSE = {"email_triage": "triage", "capture_process": "capture", "morning_brief": "brief",
               "open_loops": "draft", "promises": "promises", "pa": "draft"}


def pick(purpose: str, schedule: dict | None = None) -> str:
    override = ((schedule or {}).get("models") or {}).get(purpose)
    if override in ALIASES:
        return override
    return DEFAULTS.get(purpose, "sonnet")


def record(engine, purpose: str, usage: dict | None, ok: bool) -> None:
    u = usage or {}
    with Session(engine) as s:
        s.add(ModelRun(purpose=purpose[:40], model=(u.get("model") or None), ok=ok, duration_ms=u.get("duration_ms"),
                       input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens"), cost_usd=u.get("cost_usd")))
        s.commit()


def family(model: str | None) -> str:
    m = (model or "").lower()
    return next((a for a in ALIASES if a in m), m or "unknown")


def usage(s: Session, days: int) -> dict:
    since = utcnow() - dt.timedelta(days=days)
    rows = s.exec(select(ModelRun).where(ModelRun.ts >= since)).all()
    by_model: dict[str, dict] = {}
    by_purpose: dict[str, dict] = {}
    for r in rows:
        for key, bucket in ((family(r.model), by_model), (r.purpose, by_purpose)):
            b = bucket.setdefault(key, {"runs": 0, "minutes": 0.0, "tokens": 0, "usd": 0.0, "failed": 0})
            b["runs"] += 1
            b["minutes"] += (r.duration_ms or 0) / 60000
            b["tokens"] += (r.input_tokens or 0) + (r.output_tokens or 0)
            b["usd"] += r.cost_usd or 0
            b["failed"] += 0 if r.ok else 1
    for bucket in (by_model, by_purpose):
        for b in bucket.values():
            b["minutes"] = round(b["minutes"], 1)
            b["usd"] = round(b["usd"], 2)
    return {"days": days, "by_model": by_model, "by_purpose": dict(sorted(by_purpose.items(), key=lambda kv: -kv[1]["runs"])),
            "total_runs": len(rows)}
