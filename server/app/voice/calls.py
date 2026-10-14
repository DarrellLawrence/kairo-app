"""Kairo calls the owner (phase 3). A real phone ring needs a native iOS app
with CallKit (later, needs an Apple Developer account); until then a push
"Kairo is calling" opens the voice screen, which speaks the opening and then
listens. Unanswered after RING_MINUTES = missed."""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session, select

from ..db import Call, SettingKV, utcnow

RING_MINUTES = 15
MAX_KAIRO_CALLS_A_DAY = 1
OPTIONS = {"checkin": "Evening check-in as a call"}
IMPORTANT = ("high", "critical")
# v8 D: Kairo calls only for important things or real questions, and only in these windows. Generic
# defaults; the owner's own windows are a setting (More -> Settings, or by voice: settings_set call_windows).
DEFAULT_WINDOWS = [
    {"name": "morning commute", "days": "weekdays", "start": "06:00", "end": "08:00"},
    {"name": "evening commute", "days": "weekdays", "start": "15:30", "end": "18:30"},
    {"name": "evening at home", "days": "all", "start": "19:00", "end": "21:30"},
]


def _aware(t):
    return t if t is None or t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def start(s: Session, reason: str, opening: str, pusher, *, ignore_quiet: bool = False, by_owner: bool = False) -> Call:
    c = Call(reason=reason[:120], opening=opening[:3000], kind="owner" if by_owner or pusher is None else "kairo")
    s.add(c)
    s.commit()
    s.refresh(c)
    if pusher is not None:   # None: the owner is calling Kairo ("Call Kairo" Shortcut)
        pusher.notify("Kairo is calling", reason[:120], f"/#call/{c.id}", ignore_quiet=ignore_quiet, source="call")
    return c


def refresh(c: Call) -> Call:
    if c.status == "ringing" and utcnow() - _aware(c.created_at) > dt.timedelta(minutes=RING_MINUTES):
        c.status = "missed"
    return c


def get(s: Session, call_id: int) -> Call | None:
    c = s.get(Call, call_id)
    if c is not None:
        old = c.status
        refresh(c)
        if c.status != old:
            s.add(c)
            s.commit()
    return c


def windows(s: Session) -> list[dict]:
    from .. import prefs
    return prefs.load(s).get("call_windows") or DEFAULT_WINDOWS


def in_window(wins: list[dict], now: dt.datetime) -> dict | None:
    hhmm, weekday = now.strftime("%H:%M"), now.weekday()
    for w in wins:
        days = w.get("days", "all")
        if (days == "weekdays" and weekday >= 5) or (days == "weekend" and weekday < 5):
            continue
        if w["start"] <= hhmm < w["end"]:
            return w
    return None


def kairo_calls_on(s: Session, day: dt.date, tz) -> int:
    """Calls Kairo started that day (the owner's "call me now" and "Call Kairo" never count)."""
    start_ = dt.datetime.combine(day, dt.time.min, tzinfo=tz).astimezone(dt.timezone.utc)
    end_ = start_ + dt.timedelta(days=1)
    return len(s.exec(select(Call.id).where(Call.kind == "kairo").where(Call.created_at >= start_)
                      .where(Call.created_at < end_)).all())


def may_call(s: Session, *, importance: str, question: bool, now: dt.datetime) -> tuple[bool, str]:
    """Kairo rings only for something important or a real question, only in a call window, and at most
    once a day (fix plan item 21: one call budget for the check-in, knowledge calls and everything else)."""
    if importance not in IMPORTANT and not question:
        return False, "not important enough for a call"
    w = in_window(windows(s), now)
    if w is None:
        return False, "outside the call windows"
    if kairo_calls_on(s, now.date(), now.tzinfo or dt.timezone.utc) >= MAX_KAIRO_CALLS_A_DAY:
        return False, "Kairo already called once today"
    return True, w["name"]


def proactive(s: Session, reason: str, opening: str, pusher, *, importance: str, question: bool = False,
              now: dt.datetime, url: str = "/#today") -> Call | None:
    """Kairo wants to talk: a call if may_call() says so, else a plain push (never a ring outside the windows)."""
    ok, why = may_call(s, importance=importance, question=question, now=now)
    if ok:
        return start(s, reason, opening, pusher)
    if pusher is not None:
        pusher.notify("Kairo", reason[:120], url, source="loops")
    return None


def set_status(s: Session, call_id: int, status: str) -> Call:
    c = get(s, call_id)
    if c is None:
        raise KeyError(call_id)
    if status == "answered" and c.status == "ringing":
        c.status, c.answered_at = "answered", utcnow()
    elif status == "ended" and c.status in ("ringing", "answered"):
        c.status, c.ended_at = "ended", utcnow()
    s.add(c)
    s.commit()
    s.refresh(c)
    return c


def as_dict(c: Call) -> dict:
    return {"id": c.id, "reason": c.reason, "opening": c.opening, "status": c.status,
            "created_at": _aware(c.created_at).isoformat()}


def recent(s: Session, limit: int = 20) -> list[Call]:
    return [refresh(c) for c in s.exec(select(Call).order_by(Call.id.desc()).limit(limit)).all()]


def options(s: Session) -> dict[str, bool]:
    return {k: (s.get(SettingKV, f"call:{k}") or SettingKV(key="", value="0")).value == "1" for k in OPTIONS}


def set_option(s: Session, key: str, on: bool) -> dict[str, bool]:
    if key not in OPTIONS:
        raise KeyError(key)
    row = s.get(SettingKV, f"call:{key}") or SettingKV(key=f"call:{key}")
    row.value = "1" if on else "0"
    s.add(row)
    s.commit()
    return options(s)


def live(s: Session) -> bool:
    """A call is ringing or under way (the auto-updater waits, fix P2.14). An answered call that
    never ended counts for 3 hours at most."""
    since = utcnow() - dt.timedelta(hours=3)
    for c in s.exec(select(Call).where(Call.status.in_(("ringing", "answered")))).all():
        refresh(c)
        if c.status == "ringing" or (c.status == "answered" and _aware(c.answered_at or c.created_at) > since):
            return True
    return False
