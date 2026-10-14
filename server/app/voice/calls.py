"""Kairo calls the owner (phase 3). A real phone ring needs a native iOS app
with CallKit (later, needs an Apple Developer account); until then a push
"Kairo is calling" opens the voice screen, which speaks the opening and then
listens. Unanswered after RING_MINUTES = missed."""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session, select

from ..db import Call, SettingKV, utcnow

RING_MINUTES = 15
OPTIONS = {"brief": "Morning brief as a call", "checkin": "Evening check-in as a call"}


def _aware(t):
    return t if t is None or t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def start(s: Session, reason: str, opening: str, pusher, *, ignore_quiet: bool = False) -> Call:
    c = Call(reason=reason[:120], opening=opening[:3000])
    s.add(c)
    s.commit()
    s.refresh(c)
    pusher.notify("Kairo is calling", reason[:120], f"/#call/{c.id}", ignore_quiet=ignore_quiet)
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
