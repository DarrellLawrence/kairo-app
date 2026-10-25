"""How call turns were answered (owner's request 2026-10-16): from the Today card on the PC, or via Claude.

Every turn the voice model answers is counted: "card" (a question it answered by itself, from the card),
"claude" (it went to ask_kairo) or "talk" (small talk, no question). "Question" is read from the
transcript (a question mark or a question word), so the split is an estimate; the voice lab measures
it exactly on fixed questions. A turn a fast-path rule answered with no model at all (G10: the time, the date,
a timer, the next lesson ...) is counted as "rule" and left out of the percentages, which are about what the
voice model was asked. Voice lab and Run diagnosis show the last 7 days.
"""

from __future__ import annotations

import datetime as dt
import json

from sqlmodel import Session

from .db import SettingKV

KEY = "voice.routes"
KEEP = 1000
ROUTES = ("card", "claude", "talk", "rule")


def _load(s: Session) -> list:
    row = s.get(SettingKV, KEY)
    try:
        data = json.loads(row.value) if row and row.value else []
    except ValueError:
        data = []
    return data if isinstance(data, list) else []


def record(engine, route: str, version: str, now: dt.datetime | None = None) -> None:
    if route not in ROUTES:
        return
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        rows = _load(s)[-(KEEP - 1):] + [[now.isoformat(timespec="seconds"), route, str(version)[:40]]]
        row = s.get(SettingKV, KEY) or SettingKV(key=KEY)
        row.value = json.dumps(rows)
        s.add(row)
        s.commit()


def summary(s: Session, days: int = 7, now: dt.datetime | None = None) -> dict:
    since = (now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(days=days)
    counts = {r: 0 for r in ROUTES}
    for ts, route, _v in (r for r in _load(s) if isinstance(r, list) and len(r) == 3):
        try:
            if dt.datetime.fromisoformat(ts) >= since and route in counts:
                counts[route] += 1
        except ValueError:
            continue
    questions = counts["card"] + counts["claude"]
    return {"days": days, **counts, "questions": questions,
            "on_pc_pct": round(100 * counts["card"] / questions) if questions else None,
            "via_claude_pct": round(100 * counts["claude"] / questions) if questions else None}
