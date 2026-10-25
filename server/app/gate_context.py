"""Gate context from the database: kill switch, open cards, today's outbound, and (G13) the speaker label of the
voice turn a model call belongs to.

Shared by the backend and the PreToolUse hook. Any error here must propagate:
the callers turn it into "blocked".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .flags import FLAGS, PREFIX as FLAG_PREFIX
from .gate import VOICE_LABELS, GateContext

ORIGINS = ("owner_chat", "job", "voice_note")   # voice_note: Shortcut voice note, nobody watching

VOICE_TURN_KEY = "voice.turn"                   # settings row: the label of the voice turn Claude is running now (speaker_label.py)
VOICE_FLAG = "g13.speaker_label_gate"


def read_context(engine: Engine, origin: str, *, model_turn: bool = False) -> GateContext:
    """model_turn: this decision is for a tool call the model made (the PreToolUse hook, mcp_kairo). Only then is the
    speaker label of the running voice turn read. The owner's own taps in the app and the jobs use the same gate as
    a rules engine; they are not the voice turn and must not inherit its label."""
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
        voice = _voice_label(conn) if model_turn and origin == "owner_chat" else None
    return GateContext(paused=paused == "1", origin=origin, open_approvals=int(open_cards), outbound_last_24h=int(outbound),
                       voice=voice)


def _voice_label(conn) -> str | None:
    """The label of the voice turn being run, or None (no voice turn, or g13.speaker_label_gate is off: today's
    behaviour). A record that is there but not readable raises: the caller blocks."""
    flag = conn.execute(text("SELECT value FROM settings WHERE key = :k"), {"k": FLAG_PREFIX + VOICE_FLAG}).scalar()
    if not (FLAGS[VOICE_FLAG][0] if flag not in ("0", "1") else flag == "1"):     # the same reading as flags.on()
        return None
    row = conn.execute(text("SELECT key, value FROM settings WHERE key = :k"), {"k": VOICE_TURN_KEY}).first()
    if row is None:
        return None
    try:
        label = json.loads(row[1])["label"]
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("the speaker label of the voice turn can't be read") from exc
    if not isinstance(label, str) or label not in VOICE_LABELS:
        raise ValueError("the speaker label of the voice turn is not one Kairo knows")
    return label
