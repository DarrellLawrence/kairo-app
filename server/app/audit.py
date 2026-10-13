"""Append-only audit log (KICKOFF 4). The database refuses UPDATE and DELETE
on audit_log (triggers in migration 0002), so a row, once written, stays."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlmodel import Session

from .db import AuditLog


def args_hash(args: Any) -> str:
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def audit(session: Session, *, actor: str, action: str, outcome: str, tier: int | None = None, args: Any = None) -> AuditLog:
    row = AuditLog(actor=actor, action=action, tier=tier, outcome=outcome[:1000],
                   args_hash=args_hash(args) if args is not None else None)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row
