"""Kill switch: "pause all". The gate denies every tool call while paused,
chat refuses new messages, and a running brain call is stopped."""

from __future__ import annotations

from sqlmodel import Session

from .db import SettingKV

KEY = "paused"


def is_paused(session: Session) -> bool:
    row = session.get(SettingKV, KEY)
    return row is not None and row.value == "1"


def set_paused(session: Session, paused: bool) -> None:
    row = session.get(SettingKV, KEY) or SettingKV(key=KEY)
    row.value = "1" if paused else "0"
    session.add(row)
    session.commit()
