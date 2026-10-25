"""Calendar truth (loops block L, owner's request 2026-10-15).

The bug: a draft said "your calendar is empty for the next 7 days" while the owner had school on
Thursday and Friday. Kairo read only the primary calendar, only today and tomorrow, and turned any
error into "(nothing)". Now: every calendar the owner shows, the days asked for, and a read that
failed says so. Nothing may state free or busy times unless the read worked. The last read and the
timetable check are kept for Run diagnosis.
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.parse
from dataclasses import dataclass, field

from sqlmodel import Session

from .db import SettingKV
from .google.client import GoogleError, GoogleNotConnected
from .jobkit import clip

LAST_KEY = "calendar.last_read"
UNREADABLE = ("THE CALENDAR COULD NOT BE READ ({why}). Say nothing about free or busy times, "
              "and don't say the calendar is empty.")


@dataclass
class CalendarRead:
    ok: bool
    events: list[dict] = field(default_factory=list)
    error: str | None = None
    calendars: int = 0
    start: dt.date | None = None
    days: int = 0

    def text(self, tz) -> str:
        if not self.ok:
            return UNREADABLE.format(why=clip(self.error or "unknown error", 120))
        if not self.events:
            return f"(no events in these {self.days} days: the calendar was read and is empty)"
        lines = []
        for e in self.events:
            if e.get("all_day"):
                lines.append(f"- {dt.date.fromisoformat(e['start'][:10]):%a %d %b} all day: {e['summary']}")
                continue
            st, en = dt.datetime.fromisoformat(e["start"]).astimezone(tz), e.get("end")
            end = f"–{dt.datetime.fromisoformat(en).astimezone(tz):%H:%M}" if en and "T" in en else ""
            lines.append(f"- {st:%a %d %b %H:%M}{end} {e['summary']}")
        return "\n".join(lines)


def _calendars(g) -> list[str]:
    items = g.get("/calendar/v3/users/me/calendarList", minAccessRole="reader").get("items", []) or []
    ids = [c["id"] for c in items if c.get("primary") or c.get("selected")]
    return ["primary"] + [i for i in ids if i != "primary" and not any(c.get("primary") and c["id"] == i for c in items)]


def read_days(google_read, tz, start: dt.date, *, days: int = 7, session: Session | None = None,
              detail: bool = False) -> CalendarRead:
    """Every shown calendar, `days` days from `start` (local). Never raises: a failed read is ok=False. detail: each event also
    carries its description and whether the owner declined it (the Today card's read, for the fast paths "next lesson" and "next
    appointment"); nobody else asks for it."""
    lo = dt.datetime.combine(start, dt.time.min, tzinfo=tz)
    hi = lo + dt.timedelta(days=days)
    out = CalendarRead(ok=False, start=start, days=days)
    try:
        g = google_read()
    except Exception as exc:   # not connected, token problem
        out.error = f"{type(exc).__name__}: {exc}"
        return _remember(session, out)
    try:
        cals = _calendars(g)
        for cal in cals:
            data = g.get(f"/calendar/v3/calendars/{urllib.parse.quote(cal, safe='')}/events", timeMin=lo.isoformat(),
                         timeMax=hi.isoformat(), singleEvents="true", orderBy="startTime", maxResults=100)
            for e in data.get("items", []) or []:
                if e.get("status") == "cancelled":
                    continue
                st, en = e.get("start") or {}, e.get("end") or {}
                item = {"summary": clip(e.get("summary") or "(no title)", 120),
                        "start": st.get("dateTime") or st.get("date"), "end": en.get("dateTime") or en.get("date"),
                        "all_day": "date" in st and "dateTime" not in st,
                        "location": clip(e.get("location"), 120) or None, "calendar": cal}
                if detail:
                    item["description"] = str(e.get("description") or "")[:1500] or None     # its line breaks matter: not clip()
                    item["declined"] = any(a.get("self") and a.get("responseStatus") == "declined" for a in e.get("attendees") or []
                                           if isinstance(a, dict))
                out.events.append(item)
        out.ok, out.calendars = True, len(cals)
    except (GoogleNotConnected, GoogleError, KeyError, ValueError) as exc:
        out.events, out.error = [], f"{type(exc).__name__}: {exc}"
    finally:
        g.close()
    out.events.sort(key=lambda e: (e["start"] or ""))
    return _remember(session, out)


def _remember(session: Session | None, r: CalendarRead) -> CalendarRead:
    if session is not None:
        row = session.get(SettingKV, LAST_KEY) or SettingKV(key=LAST_KEY)
        row.value = json.dumps({"ok": r.ok, "error": r.error, "calendars": r.calendars, "events": len(r.events),
                                "days": r.days, "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
        session.add(row)
        session.commit()
    return r


def last(s: Session) -> dict:
    row = s.get(SettingKV, LAST_KEY)
    try:
        return json.loads(row.value) if row and row.value else {}
    except ValueError:
        return {}


TIMETABLE_CHECK_KEY = "timetable.check"


def timetable_status(s: Session, google_read, *, now: dt.datetime | None = None) -> dict:
    """Is the school timetable in the calendar as weekly FIXED blocks? state: ok / not imported /
    events missing / calendar unreadable. Kept for Run diagnosis."""
    from . import timetable
    from .db import CalendarTag
    row = s.get(SettingKV, timetable.KEY)
    try:
        imp = json.loads(row.value) if row and row.value else None
    except ValueError:
        imp = None
    out: dict = {"state": "not imported", "present": 0, "missing": [], "until": None}
    if imp and imp.get("events"):
        out["until"] = imp.get("until")
        try:
            g = google_read()
        except Exception as exc:
            out.update(state="calendar unreadable", error=str(exc)[:160])
            return _keep(s, out, now)
        try:
            for e in imp["events"]:
                label = f"{e.get('day')} {e.get('start')}–{e.get('end')}"
                try:
                    ev = g.get(f"/calendar/v3/calendars/primary/events/{e['event_id']}")
                except GoogleError as exc:
                    if getattr(exc, "status", None) == 404 or "not found" in str(exc).lower():
                        out["missing"].append(label)
                        continue
                    raise
                if ev.get("status") == "cancelled":
                    out["missing"].append(label)
                    continue
                out["present"] += 1
                tag = s.get(CalendarTag, e["event_id"])
                if tag is None or tag.type != "FIXED":     # Kairo's own tag: put right at once
                    s.merge(CalendarTag(event_id=e["event_id"], type="FIXED"))
                    s.commit()
                    out["retagged"] = out.get("retagged", 0) + 1
        except (GoogleNotConnected, GoogleError) as exc:
            out.update(state="calendar unreadable", error=str(exc)[:160], present=0, missing=[])
            return _keep(s, out, now)
        finally:
            g.close()
        out["state"] = "events missing" if out["missing"] else "ok"
    return _keep(s, out, now)


def _keep(s: Session, out: dict, now: dt.datetime | None = None) -> dict:
    row = s.get(SettingKV, TIMETABLE_CHECK_KEY) or SettingKV(key=TIMETABLE_CHECK_KEY)
    row.value = json.dumps({**out, "at": (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")})
    s.add(row)
    s.commit()
    return out


def timetable_daily(s: Session, google_read, tz, *, now: dt.datetime, hold, tell=None) -> dict | None:
    """Once a day: the timetable check, and school blocks that went missing are put back (same day and
    times, weekly until the semester ends, FIXED), with one line to the owner. None: already done today."""
    from . import timetable
    row = s.get(SettingKV, TIMETABLE_CHECK_KEY)
    try:
        last = json.loads(row.value).get("at") if row and row.value else None
    except ValueError:
        last = None
    if last and dt.datetime.fromisoformat(last).astimezone(tz).date() == now.astimezone(tz).date():
        return None
    st = timetable_status(s, google_read, now=now)
    if st["state"] != "events missing" or hold is None:
        return st
    imp_row = s.get(SettingKV, timetable.KEY)
    imp = json.loads(imp_row.value)
    until = dt.date.fromisoformat(imp["until"])
    until_utc = dt.datetime.combine(until, dt.time(23, 59, 59), tzinfo=tz).astimezone(dt.timezone.utc)
    put_back = []
    today = now.astimezone(tz).date()
    for e in imp["events"]:
        label = f"{e.get('day')} {e.get('start')}–{e.get('end')}"
        if label not in st["missing"] or e.get("day") not in timetable.DAYS:
            continue
        first = today + dt.timedelta(days=(timetable.DAYS.index(e["day"]) - today.weekday()) % 7)
        start = dt.datetime.combine(first, dt.time.fromisoformat(e["start"]), tzinfo=tz)
        end = dt.datetime.combine(first, dt.time.fromisoformat(e["end"]), tzinfo=tz)
        if start <= now:
            start, end = start + dt.timedelta(days=7), end + dt.timedelta(days=7)
        eid = hold({"summary": "School", "description": "Put back by Kairo (it was missing from the calendar)",
                    "start": start.isoformat(), "end": end.isoformat(), "time_zone": str(tz),
                    "recurrence": [f"RRULE:FREQ=WEEKLY;BYDAY={e['day']};UNTIL={until_utc:%Y%m%dT%H%M%SZ}"]},
                   "school timetable: a block went missing")
        if eid:
            e["event_id"] = eid
            put_back.append(label)
    imp_row.value = json.dumps(imp)
    s.add(imp_row)
    s.commit()
    if put_back and tell:
        tell(f"School was missing from your calendar ({', '.join(put_back)}). I put it back as fixed weekly blocks.")
    out = {**st, "state": "repaired" if put_back else st["state"], "put_back": put_back}
    _keep(s, out, now)
    return out
