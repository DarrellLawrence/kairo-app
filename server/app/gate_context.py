"""Gate context from the database: kill switch, open cards, today's outbound.

Shared by the backend and the PreToolUse hook. Any error here must propagate:
the callers turn it into "blocked".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .gate import GateContext

ORIGINS = ("owner_chat", "job", "voice_note")   # voice_note: Shortcut voice note, nobody watching


def read_context(engine: Engine, origin: str) -> GateContext:
    if origin not in ORIGINS:
        raise ValueError(f"unknown origin {origin!r}")
    # Same text format SQLAlchemy stores datetimes in on SQLite (avoids the deprecated sqlite3 adapter).
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S.%f")
    with engine.connect() as conn:
        paused = conn.execute(text("SELECT value FROM settings WHERE key = 'paused'")).scalar()
        open_cards = conn.execute(text("SELECT COUNT(*) FROM approvals WHERE status = 'open'")).scalar_one()
        outbound = conn.execute(
            text("SELECT COUNT(*) FROM approvals WHERE tier = 3 AND status = 'done' AND decided_at >= :since"),
            {"since": since},
        ).scalar_one()
    return GateContext(paused=paused == "1", origin=origin, open_approvals=int(open_cards), outbound_last_24h=int(outbound))
