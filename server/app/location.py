"""Location, privacy first (owner's request 2026-10-14).

- iPhone Shortcut automations send "arrive/leave <place>" to
  POST /api/location/event with the location token. Only the place NAME and
  the time are stored, only on kairo-home, never sent anywhere.
- The owner can pause it, see the history and delete any or all of it.
  Events older than KEEP_DAYS are deleted automatically.
- Kairo uses it for: reminders tied to a place (task.place, pushed on
  arrival), the brief (where you are / usually are) and patterns (e.g. "you
  skip training on long school days") in the brief and the reviews.
"""

from __future__ import annotations

import datetime as dt
import hmac
import re
import secrets
import statistics
from collections import defaultdict
from pathlib import Path

from sqlmodel import Session, delete, select

from .db import LocationEvent, SettingKV, Task, utcnow

TOKEN_FILE = "location-token.txt"
PAUSED_KEY = "location_paused"
KEEP_DAYS = 365
DEDUPE = dt.timedelta(minutes=3)
LONG_DAY_H = 7.0
DAY_PLACES = ("School", "Work")   # where a "long day" happens
_PLACE = re.compile(r"^[\w][\w .'&-]{0,39}$", re.UNICODE)


class LocationError(ValueError):
    pass


# ---------------------------------------------------------------- token + pause

def token(secrets_dir: Path) -> str:
    path = Path(secrets_dir) / TOKEN_FILE
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(24), encoding="ascii")
    return path.read_text(encoding="ascii").strip()


def check_token(secrets_dir: Path, header: str | None) -> bool:
    sent = (header or "").removeprefix("Bearer ").strip()
    return bool(sent) and hmac.compare_digest(sent, token(secrets_dir))


def rotate_token(secrets_dir: Path) -> str:
    (Path(secrets_dir) / TOKEN_FILE).unlink(missing_ok=True)
    return token(secrets_dir)


def is_paused(s: Session) -> bool:
    row = s.get(SettingKV, PAUSED_KEY)
    return bool(row and row.value == "1")


def set_paused(s: Session, paused: bool) -> None:
    row = s.get(SettingKV, PAUSED_KEY) or SettingKV(key=PAUSED_KEY)
    row.value = "1" if paused else "0"
    s.add(row)
    s.commit()


# ---------------------------------------------------------------- events

def clean_place(place: str) -> str:
    p = " ".join(str(place or "").split())
    if not _PLACE.match(p):
        raise LocationError("place must be a short name like Home, School, Work or Gym")
    return p[:1].upper() + p[1:]


def _aware(ts: dt.datetime) -> dt.datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)


def record(s: Session, *, place: str, event: str, ts: dt.datetime | None = None) -> LocationEvent | None:
    """Store one event. None when paused (nothing is kept) or a duplicate."""
    if event not in ("arrive", "leave"):
        raise LocationError("event must be arrive or leave")
    place = clean_place(place)
    now = utcnow()
    ts = _aware(ts) if ts else now
    if ts > now + dt.timedelta(minutes=5) or ts < now - dt.timedelta(days=2):
        raise LocationError("time must be within the last two days")
    if is_paused(s):
        return None
    last = s.exec(select(LocationEvent).where(LocationEvent.place == place).order_by(LocationEvent.ts.desc())).first()
    if last and last.event == event and abs(_aware(last.ts) - ts) < DEDUPE:
        return None   # the Shortcut fired twice
    row = LocationEvent(place=place, event=event, ts=ts)
    s.add(row)
    s.commit()
    s.refresh(row)
    prune(s)
    return row


def prune(s: Session, keep_days: int = KEEP_DAYS) -> int:
    cutoff = utcnow() - dt.timedelta(days=keep_days)
    n = s.exec(select(LocationEvent).where(LocationEvent.ts < cutoff)).all()
    if n:
        s.exec(delete(LocationEvent).where(LocationEvent.ts < cutoff))
        s.commit()
    return len(n)


def history(s: Session, limit: int = 200) -> list[LocationEvent]:
    return list(s.exec(select(LocationEvent).order_by(LocationEvent.ts.desc()).limit(limit)).all())


def delete_one(s: Session, event_id: int) -> bool:
    row = s.get(LocationEvent, event_id)
    if row is None:
        return False
    s.delete(row)
    s.commit()
    return True


def delete_all(s: Session) -> int:
    n = len(s.exec(select(LocationEvent.id)).all())
    s.exec(delete(LocationEvent))
    s.commit()
    return n


def as_dict(e: LocationEvent) -> dict:
    return {"id": e.id, "place": e.place, "event": e.event, "ts": _aware(e.ts).isoformat()}


def current_place(s: Session) -> str | None:
    last = s.exec(select(LocationEvent).order_by(LocationEvent.ts.desc())).first()
    if last is None or last.event != "arrive" or utcnow() - _aware(last.ts) > dt.timedelta(hours=16):
        return None
    return last.place


def places(s: Session) -> list[str]:
    return sorted(set(s.exec(select(LocationEvent.place)).all()))


COMMUTE = dt.timedelta(minutes=90)   # left a place this recently and not arrived anywhere yet: on the way


def whereabouts(s: Session, now: dt.datetime, home=("home",)) -> str | None:
    """"home", "out" or "commute" from the last location event (a personal reminder waits for a good moment,
    owner's request 2026-10-20). None when Kairo can't tell: no events, paused, or the last one is old.
    home: the names of the owner's home places (lower case)."""
    if is_paused(s):
        return None
    last = s.exec(select(LocationEvent).where(LocationEvent.ts <= now.astimezone(dt.timezone.utc))
                  .order_by(LocationEvent.ts.desc())).first()
    if last is None or now - _aware(last.ts) > dt.timedelta(hours=16):
        return None
    at_home = last.place.lower() in home
    if last.event == "arrive":
        return "home" if at_home else "out"
    if now - _aware(last.ts) <= COMMUTE:
        return "commute"
    return "out" if at_home else None   # left home and never came back; left school: maybe home, unsaid


# ---------------------------------------------------------------- reminders

def tasks_for(s: Session, place: str) -> list[Task]:
    return list(s.exec(select(Task).where(Task.status.in_(["open", "doing"])).where(Task.place == place)).all())


def arrival_reminder(s: Session, place: str) -> tuple[str, str] | None:
    """(title, body) for a push when the owner arrives somewhere with tasks waiting there."""
    tasks = tasks_for(s, place)
    if not tasks:
        return None
    titles = [t.title for t in tasks[:4]]
    more = f" (+{len(tasks) - 4} more)" if len(tasks) > 4 else ""
    return f"At {place}", "; ".join(titles) + more


# ---------------------------------------------------------------- patterns

def visits(events: list[LocationEvent], tz) -> list[tuple[str, dt.datetime, dt.datetime]]:
    """Pair arrive -> leave per place (local time). Unpaired events are dropped."""
    open_: dict[str, dt.datetime] = {}
    out = []
    for e in sorted(events, key=lambda e: _aware(e.ts)):
        t = _aware(e.ts).astimezone(tz)
        if e.event == "arrive":
            open_[e.place] = t
        elif e.place in open_:
            start = open_.pop(e.place)
            if dt.timedelta(minutes=5) <= t - start <= dt.timedelta(hours=16):
                out.append((e.place, start, t))
    return out


def _hhmm(minutes: float) -> str:
    m = int(round(minutes))
    return f"{m // 60:02d}:{m % 60:02d}"


def patterns(s: Session, tz, *, weeks: int = 8, today: dt.date | None = None) -> list[str]:
    """Plain facts from the last weeks. Only said when there is enough data."""
    today = today or dt.datetime.now(tz).date()
    since = dt.datetime.combine(today - dt.timedelta(weeks=weeks), dt.time.min, tzinfo=tz)
    events = s.exec(select(LocationEvent).where(LocationEvent.ts >= since.astimezone(dt.timezone.utc))).all()
    vs = visits(list(events), tz)
    if not vs:
        return []
    facts: list[str] = []
    by_place: dict[str, list] = defaultdict(list)
    for place, a, b in vs:
        by_place[place].append((a, b))
    days_seen = {a.date() for _, a, _ in vs}
    n_weeks = max(1, len({d.isocalendar()[:2] for d in days_seen}))   # calendar weeks with any data
    for place, lst in sorted(by_place.items()):
        if place == "Home" or len(lst) < 3:
            continue
        per_week = len(lst) / n_weeks
        arrive = statistics.median(a.hour * 60 + a.minute for a, _ in lst)
        stay = statistics.median((b - a).total_seconds() / 3600 for a, b in lst)
        facts.append(f"{place}: about {per_week:.1f} times a week, usually arriving around {_hhmm(arrive)}, staying {stay:.1f} h.")
    # Long days at school/work vs the other places on those days.
    day_hours: dict[dt.date, float] = defaultdict(float)
    for place, a, b in vs:
        if place in DAY_PLACES:
            day_hours[a.date()] += (b - a).total_seconds() / 3600
    weekdays = {d for d in days_seen if d.weekday() < 5}
    long_days = {d for d in weekdays if day_hours.get(d, 0) >= LONG_DAY_H}
    other_days = weekdays - long_days
    if len(long_days) >= 3 and len(other_days) >= 3:
        for place, lst in sorted(by_place.items()):
            if place in DAY_PLACES or place == "Home":
                continue
            went = {a.date() for a, _ in lst}
            on_long = len(went & long_days) / len(long_days)
            on_other = len(went & other_days) / len(other_days)
            if abs(on_long - on_other) >= 0.3:
                more_less = "less" if on_long < on_other else "more"
                facts.append(f"{place} on long days: you go {more_less} often on long school/work days (7 h or more), "
                             f"{on_long:.0%} of those days vs {on_other:.0%} of other weekdays.")
    return facts
