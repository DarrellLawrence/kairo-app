"""Kairo puts things in the owner's own calendar by itself (tier 2, v7): a hold through the gate
(own calendar, [Kairo] prefix, no guests, sendUpdates none), run by the executor, tagged by type.
Used by the timetable import and by promises to attend (loops block L)."""

from __future__ import annotations

import logging

from sqlmodel import Session

from .db import CalendarTag

log = logging.getLogger("kairo.own_calendar")


def hold(engine, gate, executor, args: dict, *, reason: str, tag: str = "FIXED", origin: str = "job") -> str | None:
    """The new event's id, or None when the gate said no or Google failed (logged)."""
    from .gate import TOOL_PREFIX
    from .gate_context import read_context
    d = gate.decide(TOOL_PREFIX + "propose_action", {"action": "calendar.create_hold", "args": args, "reason": reason},
                    read_context(engine, origin))
    if d.verdict != "execute":
        log.warning("calendar hold denied (%s): %s", reason, d.reason)
        return None
    ex = executor() if callable(executor) and not hasattr(executor, "run") else executor
    try:
        res = ex.run(d.action, d.args)
    except Exception as exc:   # Google down: the caller falls back (a reminder instead of an event)
        log.warning("calendar hold failed (%s): %s", reason, exc)
        return None
    eid = res.get("event_id")
    if eid:
        with Session(engine) as s:
            s.merge(CalendarTag(event_id=eid, type=tag))
            s.commit()
    return eid
