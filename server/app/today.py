"""The Today screen in one call (v7 redesign): summary line, check-in streak,
snapshot numbers, top 3, next up, "try asking" chips built from the real day,
and a short list of problems (the full status lives in More -> System).
Only Kairo's own data plus the read-only calendar; no model call, so it is fast.
"""

from __future__ import annotations

import datetime as dt
import re

from sqlmodel import Session, select

from . import brief as brief_mod
from . import triage
from .db import Checkin, HealthDay, SchoolEvent, Task, utcnow
from .jobkit import clip

WEEK = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _local(t: dt.datetime | None, tz) -> dt.datetime | None:
    if t is None:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).astimezone(tz)


def streak(s: Session, today: dt.date) -> dict:
    """Days in a row with a daily check-in. Today still counts as open until the evening."""
    days = {c.date for c in s.exec(select(Checkin).where(Checkin.date >= today - dt.timedelta(days=400))).all()}
    n, d = 0, today if today in days else today - dt.timedelta(days=1)
    while d in days:
        n += 1
        d -= dt.timedelta(days=1)
    monday = today - dt.timedelta(days=today.weekday())
    week = []
    for i, name in enumerate(WEEK):
        day = monday + dt.timedelta(days=i)
        state = "done" if day in days else ("today" if day == today else ("missed" if day < today else "future"))
        week.append({"day": name, "date": day.isoformat(), "state": state})
    return {"count": n, "done_today": today in days, "week": week}


def top3(s: Session, today: dt.date, tz) -> list[dict]:
    """Overdue first, then due today, then highest priority. Real tasks, so they can be ticked off."""
    open_ = s.exec(select(Task).where(Task.status.in_(["open", "doing"]))).all()
    end = dt.datetime.combine(today, dt.time.max, tzinfo=tz)

    def rank(t: Task):
        due = _local(t.due, tz)
        return (0 if due and due <= end else 1, due or dt.datetime.max.replace(tzinfo=tz), -t.priority, t.id)

    out = []
    for t in sorted(open_, key=rank)[:3]:
        due = _local(t.due, tz)
        why = None
        if due and due.date() < today:
            why = "overdue"
        elif due and due.date() == today:
            why = "due today"
        elif due:
            why = "due " + due.strftime("%a")
        out.append({"id": t.id, "title": t.title, "why": why, "done": False})
    return out


def _sender_name(sender: str | None) -> str:
    name = re.sub(r"<[^>]*>", "", sender or "").strip().strip('"')
    return clip(name or (sender or "someone"), 30)


def suggestions(*, next_event: dict | None, replies: list, school: list, overdue: int, sleep_h, today: dt.date) -> list[dict]:
    """'Try asking' chips from the real day (no generic examples). Each has its own glow."""
    out = []
    if replies:
        out.append({"text": f"Draft a reply to {_sender_name(replies[0].sender)}", "glow": "teal",
                    "prompt": f"Draft a reply to the mail from {replies[0].sender} about \"{replies[0].subject}\"."})
    if school:
        e = school[0]
        out.append({"text": f"Quiz me for {e.subject}", "glow": "green",
                    "prompt": f"Quiz me for my {e.subject} {e.kind} on {_local(e.date, dt.timezone.utc):%A}."})
    if next_event:
        out.append({"text": f"Prep me for {clip(next_event['title'], 28)}", "glow": "ember",
                    "prompt": f"What do I need for \"{next_event['title']}\" at {next_event['time']}?"})
    if overdue:
        out.append({"text": f"Plan my {overdue} overdue task{'s' if overdue != 1 else ''}", "glow": "red",
                    "prompt": "Look at my overdue tasks and plan when I do each one."})
    if sleep_h is not None and sleep_h < 6.5:
        out.append({"text": f"Only {sleep_h:.1f} h sleep. Adjust today?", "glow": "magenta",
                    "prompt": f"I slept {sleep_h:.1f} hours. Make today lighter: what can move?"})
    out.append({"text": "What should I focus on now?", "glow": "ember", "prompt": "What should I focus on right now?"})
    return out[:4]


def home(s: Session, *, profile, events: list[dict], calendar_error: str | None, issues: list[str],
         now: dt.datetime | None = None) -> dict:
    tz = profile.tz
    now = (now or dt.datetime.now(tz)).astimezone(tz)
    today = now.date()
    day_start = dt.datetime.combine(today, dt.time.min, tzinfo=tz)

    done_today = [t for t in s.exec(select(Task).where(Task.status == "done")).all()
                  if t.done_at and _local(t.done_at, tz) >= day_start]
    todays = [e for e in events if not e.get("all_day") and (e.get("start") or "")[:10] == today.isoformat()]
    upcoming = sorted((e for e in events if not e.get("all_day") and e.get("start")
                       and dt.datetime.fromisoformat(e["start"]).astimezone(tz) > now),
                      key=lambda e: dt.datetime.fromisoformat(e["start"]))
    next_up = None
    if upcoming:
        e = upcoming[0]
        start = dt.datetime.fromisoformat(e["start"]).astimezone(tz)
        next_up = {"title": e["summary"], "time": start.strftime("%H:%M"), "start": start.isoformat(),
                   "location": e.get("location"), "in_minutes": int((start - now).total_seconds() // 60),
                   "tomorrow": start.date() != today}
    health = s.get(HealthDay, today) or s.get(HealthDay, today - dt.timedelta(days=1))
    sleep_h = health.sleep_h if health else None
    overdue = len([t for t in s.exec(select(Task).where(Task.status.in_(["open", "doing"]))).all()
                   if t.due and _local(t.due, tz) < day_start])
    replies = triage.needs_reply(s, limit=3)
    school = list(s.exec(select(SchoolEvent).where(SchoolEvent.date >= utcnow())
                         .where(SchoolEvent.date <= utcnow() + dt.timedelta(days=7)).order_by(SchoolEvent.date)).all())

    b = brief_mod.latest(s, today)
    if b and b.get("date", today.isoformat()) == today.isoformat() and b.get("headline"):
        summary = b["headline"]
    else:
        bits = [f"{len(todays)} appointment{'s' if len(todays) != 1 else ''}"]
        if overdue:
            bits.append(f"{overdue} overdue")
        if replies:
            bits.append(f"{len(replies)} mail{'s' if len(replies) != 1 else ''} to answer")
        summary = "Today: " + ", ".join(bits) + "."
    from . import loops as loops_mod
    open_ = loops_mod.open_loops(s)
    return {
        "loops": {"count": len(open_), "urgent": sum(1 for l in open_ if l.urgency == "urgent"),
                  "top": [loops_mod.as_dict(l, now, tz) for l in open_[:3]]},
        "name": profile.name.split()[0],
        "date": today.isoformat(),
        "summary": summary,
        "streak": streak(s, today),
        "snapshot": [
            {"label": "Done today", "value": len(done_today)},
            {"label": "Events", "value": len(todays)},
            {"label": "Sleep", "value": None if sleep_h is None else round(sleep_h, 1), "unit": "h"},
        ],
        "top3": top3(s, today, tz),
        "next_up": next_up,
        "suggestions": suggestions(next_event=next_up, replies=replies, school=school, overdue=overdue,
                                   sleep_h=sleep_h, today=today),
        "issues": issues + ([f"Calendar: {calendar_error}"] if calendar_error else []),
    }
