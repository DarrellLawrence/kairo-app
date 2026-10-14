"""Debrief ("debrief me", "what's going on"): PA rule 1.

The scope follows the time of day unless the owner names one:
  morning  today only
  day      rest of today + the week + important dates coming up
  night    tomorrow + rest of the week + dates
  everything (bored, "tell me everything"): calendar, mail, school, people,
           money, health, birthdays, holidays
A named topic ("just school") returns only that. Known routine (recurring
events that didn't change) is left out and only counted. Kairo remembers when
the owner usually asks and gives the full picture (today + week + dates) then.
"""

from __future__ import annotations

import datetime as dt
import json

from sqlmodel import Session, select

from . import life, triage
from .db import Approval, SchoolEvent, SettingKV, Task
from .jobkit import clip

HABIT_KEY = "debrief.hours"
TOPICS = ("calendar", "mail", "school", "tasks", "people", "money", "health", "loops")
# Fixed-date days worth a heads-up (not personal: the same for everyone here).
HOLIDAYS = {(1, 1): "New Year", (2, 14): "Valentine's Day", (8, 1): "Swiss National Day", (10, 31): "Halloween",
            (12, 24): "Christmas Eve", (12, 25): "Christmas", (12, 31): "New Year's Eve"}
HOW = {
    "morning": "Morning debrief: today only. Lead with what's new, unusual, at risk or easy to forget.",
    "morning_full": "Morning debrief, the full picture (the owner usually asks now): today, then the week, then dates coming up.",
    "day": "Daytime debrief: the rest of today, then the week, then important dates coming up.",
    "night": "Evening debrief: tomorrow first, then the rest of the week, then dates coming up.",
    "everything": "Full rundown, everything below, grouped. Still short per item.",
    "topic": "Only the topic asked for.",
}


def part_of_day(now: dt.datetime) -> str:
    return "morning" if now.hour < 11 else "day" if now.hour < 18 else "night"


def note_habit(s: Session, now: dt.datetime) -> None:
    row = s.get(SettingKV, HABIT_KEY) or SettingKV(key=HABIT_KEY, value="[]")
    hours = (json.loads(row.value or "[]") + [now.hour])[-20:]
    row.value = json.dumps(hours)
    s.add(row)
    s.commit()


def usual_part(s: Session) -> str | None:
    """When the owner usually asks (at least 5 asks, 60 % in one part of the day)."""
    row = s.get(SettingKV, HABIT_KEY)
    hours = json.loads(row.value) if row and row.value else []
    if len(hours) < 5:
        return None
    parts = [("morning" if h < 11 else "day" if h < 18 else "night") for h in hours]
    best = max(set(parts), key=parts.count)
    return best if parts.count(best) / len(parts) >= 0.6 else None


def fetch_events(google_read, start: dt.datetime, end: dt.datetime) -> tuple[list[dict], str | None]:
    try:
        g = google_read()
        try:
            data = g.get("/calendar/v3/calendars/primary/events", timeMin=start.isoformat(), timeMax=end.isoformat(),
                         singleEvents="true", orderBy="startTime", maxResults=100)
        finally:
            g.close()
    except Exception as exc:   # not connected / Google down: the rest still works
        return [], str(exc)
    out = []
    for e in data.get("items", []) or []:
        st, en = e.get("start") or {}, e.get("end") or {}
        orig = (e.get("originalStartTime") or {}).get("dateTime")
        out.append({"summary": clip(e.get("summary") or "(no title)", 120), "start": st.get("dateTime") or st.get("date"),
                    "end": en.get("dateTime") or en.get("date"), "all_day": "date" in st and "dateTime" not in st,
                    "location": clip(e.get("location"), 120) or None,
                    "routine": bool(e.get("recurringEventId")) and (not orig or orig == st.get("dateTime")),
                    "changed": bool(orig and orig != st.get("dateTime"))})
    return out, None


def _local_date(iso: str, tz) -> dt.date:
    if len(iso) == 10:
        return dt.date.fromisoformat(iso)
    return dt.datetime.fromisoformat(iso).astimezone(tz).date()


def gather(s: Session, *, now: dt.datetime, tz, google_read, scope: str = "auto", topic: str | None = None) -> dict:
    today = now.date()
    if scope == "everything":
        mode = "everything"
    elif topic:
        mode = "topic"
    elif scope == "today":
        mode = "morning"
    elif scope == "week":
        mode = "day"
    else:
        mode = part_of_day(now)
        if mode == "morning" and usual_part(s) == "morning":
            mode = "morning_full"
    note_habit(s, now)

    if mode == "morning":
        start, end = now, dt.datetime.combine(today, dt.time.max, tzinfo=tz)
    elif mode == "night":
        start = dt.datetime.combine(today + dt.timedelta(days=1), dt.time.min, tzinfo=tz)
        end = dt.datetime.combine(today + dt.timedelta(days=7), dt.time.max, tzinfo=tz)
    else:
        start, end = now, dt.datetime.combine(today + dt.timedelta(days=7), dt.time.max, tzinfo=tz)
    events, cal_error = fetch_events(google_read, start, end)
    shown = [e for e in events if not e["routine"]]
    data: dict = {"mode": mode, "how": HOW[mode], "now": now.strftime("%A %H:%M"),
                  "calendar": [{k: e[k] for k in ("summary", "start", "end", "all_day", "location", "changed")} for e in shown],
                  "routine_left_out": len(events) - len(shown)}
    if cal_error:
        data["calendar_error"] = cal_error

    horizon = 30 if mode != "morning" else 0
    open_tasks = s.exec(select(Task).where(Task.status.in_(["open", "doing"]))).all()
    task_end = end if mode in ("morning", "night") else end
    data["tasks"] = [{"id": t.id, "title": t.title, "due": t.due.isoformat() if t.due else None}
                     for t in open_tasks if t.due and t.due.replace(tzinfo=t.due.tzinfo or dt.timezone.utc) <= task_end][:10]
    data["mail_to_answer"] = [{"from": m.sender, "subject": m.subject, "summary": m.summary} for m in triage.needs_reply(s, limit=5)]
    from . import loops
    data["open_loops"] = [loops.phrase(l, now, tz) for l in loops.open_loops(s)[:3]]
    if horizon or mode == "everything":
        days = 90 if mode == "everything" else horizon
        until = today + dt.timedelta(days=days)
        data["dates"] = [{"what": f"{e.subject} {e.kind}", "date": e.date.isoformat()} for e in
                         s.exec(select(SchoolEvent).where(SchoolEvent.date >= now.astimezone(dt.timezone.utc))
                                .where(SchoolEvent.date <= dt.datetime.combine(until, dt.time.max, tzinfo=tz))
                                .order_by(SchoolEvent.date)).all()]
        data["birthdays"] = [{"name": p.name, "date": life.next_birthday(p.birthday, today).isoformat()}
                             for p in life.people_list(s) if p.birthday and life.next_birthday(p.birthday, today) <= until]
        data["holidays"] = [{"name": n, "date": d.isoformat()} for (m, dd), n in HOLIDAYS.items()
                            for d in [dt.date(today.year + (1 if (m, dd) < (today.month, today.day) else 0), m, dd)] if d <= until]
    if mode == "everything":
        data["people"] = life.people_reminders(s, today)
        data["money_30d"] = life.money_summary(s, 30)
        data["health_7d"] = life.health_summary(s, 7, today)
        data["approvals_open"] = len(s.exec(select(Approval).where(Approval.status == "open")).all())

    if topic:
        keep = {"calendar": ("calendar", "routine_left_out"), "mail": ("mail_to_answer",), "school": ("dates",),
                "tasks": ("tasks",), "people": ("birthdays",), "money": (), "health": (), "loops": ("open_loops",)}[topic]
        if topic == "school":
            data.setdefault("dates", [{"what": f"{e.subject} {e.kind}", "date": e.date.isoformat()} for e in
                                      s.exec(select(SchoolEvent).where(SchoolEvent.date >= now.astimezone(dt.timezone.utc))
                                             .order_by(SchoolEvent.date).limit(10)).all()])
        if topic == "money":
            data["money_30d"] = life.money_summary(s, 30)
            keep = ("money_30d",)
        if topic == "health":
            data["health_7d"] = life.health_summary(s, 7, today)
            keep = ("health_7d",)
        if topic == "people":
            data["people"] = life.people_reminders(s, today)
            data.setdefault("birthdays", [])
            keep = ("people", "birthdays")
        data = {k: v for k, v in data.items() if k in ("mode", "how", "now", *keep)}
        data["topic"] = topic
    return data
