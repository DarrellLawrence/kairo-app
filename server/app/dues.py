"""How Kairo words a due date for the owner, everywhere (owner's request 2026-10-20: a deadline that had
passed still read "due Fri").

By the owner's calendar day (profile.tz), never by the clock time: a date-only due is stored as noon
(tools._due, captures, triage) or 18:00 (loops), so it reads "due today" all day and "overdue" only from
the next day on.

  past              overdue since yesterday / overdue since Fri / overdue 9 days
  today, tomorrow   due today / due tomorrow
  within 6 days     due Fri
  later             due 30 Oct
"""

from __future__ import annotations

import datetime as dt


def _day(t: dt.datetime, tz) -> dt.date:
    return (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).astimezone(tz).date()


def days_left(due: dt.datetime, now: dt.datetime, tz) -> int:
    """Calendar days from today to the due day in the owner's timezone (negative: overdue)."""
    return (_day(due, tz) - _day(now, tz)).days


def overdue(due: dt.datetime | None, now: dt.datetime, tz) -> bool:
    return due is not None and days_left(due, now, tz) < 0


def words(due: dt.datetime, now: dt.datetime, tz) -> str:
    days = days_left(due, now, tz)
    d = _day(due, tz)
    if days < 0:
        if days == -1:
            return "overdue since yesterday"
        return f"overdue since {d:%a}" if days > -7 else f"overdue {-days} days"
    if days == 0:
        return "due today"
    if days == 1:
        return "due tomorrow"
    return f"due {d:%a}" if days <= 6 else f"due {d.day} {d:%b}"
