"""Which Claude model does which job (v7, v8 D): the cheapest one that does it well, never a weak
one for judgment.

Haiku: triage, classification, summaries, reminders, simple replies.
Sonnet: writing, drafts, documents, self-check loops, training/food plans (coach), and judgment
        calls: re-planning after a change, whether to push or call, how hard to push.
Opus: important judgment calls, money coaching, investing and the monthly money review, big
      planning and weekly-or-longer reviews.
schedule.yaml / More -> Settings can override a purpose under `models:`, but never down to Haiku
for judgment, and never below Opus for money.
"""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session, func, select

from .db import ModelRun, utcnow

ALIASES = ("haiku", "sonnet", "opus")
DEFAULTS = {
    "chat": "sonnet", "call": "haiku", "voice_note": "sonnet",
    "triage": "haiku", "capture": "haiku", "letters": "haiku", "brief": "haiku", "reminder": "haiku",
    "summary": "haiku", "promises": "haiku", "reply_check": "haiku",
    "draft": "sonnet", "document": "sonnet", "gauntlet": "sonnet", "study": "sonnet", "lerndoku": "sonnet",
    "voice_check": "sonnet", "acceptance": "sonnet", "coach": "sonnet",
    "proactive": "sonnet", "judgment": "sonnet", "judgment_high": "opus",
    "money": "opus", "money_review": "opus",
    "plan": "opus", "review_big": "opus",
}
JOB_PURPOSE = {"email_triage": "triage", "capture_process": "capture", "morning_brief": "brief",
               "open_loops": "draft", "promises": "promises", "pa": "judgment", "coach": "coach"}
NEVER_HAIKU = {"proactive", "judgment", "judgment_high", "money", "money_review", "coach", "plan", "review_big"}
OPUS_ONLY = {"judgment_high", "money", "money_review"}
RANK = {"haiku": 0, "sonnet": 1, "opus": 2}


def judgment(importance: str | None) -> str:
    """The purpose for a judgment call: Opus when it matters a lot, else Sonnet. Never Haiku."""
    return "judgment_high" if importance in ("high", "critical") else "judgment"


def pick(purpose: str, schedule: dict | None = None) -> str:
    default = DEFAULTS.get(purpose, "sonnet")
    override = ((schedule or {}).get("models") or {}).get(purpose)
    if override not in ALIASES:
        return default
    if purpose in OPUS_ONLY and RANK[override] < RANK["opus"]:
        return default                     # money and important judgment stay on Opus
    if purpose in NEVER_HAIKU and override == "haiku":
        return default                     # judgment is never Haiku's
    return override


def record(engine, purpose: str, usage: dict | None, ok: bool) -> None:
    u = usage or {}
    with Session(engine) as s:
        s.add(ModelRun(purpose=purpose[:40], model=(u.get("model") or None), ok=ok, duration_ms=u.get("duration_ms"),
                       input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens"), cost_usd=u.get("cost_usd"),
                       startup_ms=u.get("startup_ms"), first_text_ms=u.get("first_text_ms"), warm=u.get("warm")))
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
