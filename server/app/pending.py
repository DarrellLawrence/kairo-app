"""What waits on the owner in Kairo's own app, and tidying it up (owner's request 2026-11-04: "can Kairo clear stuff?").

Two piles grow when the owner is busy: the quick questions about captures and imports ("Kairo has a question" on
Today) and the getting-to-know-you questions. Kairo lists them (pending.list, tier 0) and clears them when the owner
says so (pending.clear, tier 1). Nothing is deleted: a cleared item only changes status, and pending.restore brings it
back by id. Only Kairo's own tables are touched; nothing reaches another person."""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session, select

from .db import Capture, KnowledgeItem, utcnow
from .jobkit import clip

KINDS = ("quick_question", "knowledge_question")
LIST_LIMIT = 50


class PendingError(Exception):
    pass


def _aware(d: dt.datetime | None) -> dt.datetime | None:
    return d.replace(tzinfo=dt.timezone.utc) if d is not None and d.tzinfo is None else d


def _age_days(d: dt.datetime | None, now: dt.datetime) -> int:
    return max(0, (now - (_aware(d) or now)).days)


def _open_rows(s: Session, kind: str):
    if kind == "quick_question":
        return s.exec(select(Capture).where(Capture.status == "asking").order_by(Capture.id)).all()
    if kind == "knowledge_question":
        return s.exec(select(KnowledgeItem).where(KnowledgeItem.kind == "question")
                      .where(KnowledgeItem.status == "open").order_by(KnowledgeItem.id)).all()
    raise PendingError(f"unknown kind {kind!r}")


def summary(s: Session, now: dt.datetime | None = None) -> dict:
    """Counts and the items themselves (oldest first, at most LIST_LIMIT per kind)."""
    now = now or utcnow()
    out: dict = {}
    for kind in KINDS:
        rows = _open_rows(s, kind)
        items = []
        for r in rows[:LIST_LIMIT]:
            text = r.question if kind == "quick_question" else r.question
            items.append({"id": r.id, "text": clip(text or "", 200), "age_days": _age_days(r.created_at, now)})
        out[kind] = {"count": len(rows), "items": items}
    return out


def clear(s: Session, kind: str, *, ids: list[int] | None = None, all_open: bool = False,
          older_than_days: int | None = None, now: dt.datetime | None = None) -> list[int]:
    """Clear open items of one kind: the ids given, or every open one (optionally only those older than N days).
    Returns the ids cleared, so they can be restored."""
    if not ids and not all_open:
        raise PendingError("say which: ids, or all")
    now = now or utcnow()
    wanted = set(ids or [])
    done = []
    for r in _open_rows(s, kind):
        if wanted and r.id not in wanted:
            continue
        if older_than_days is not None and _age_days(r.created_at, now) < older_than_days:
            continue
        if kind == "quick_question":
            r.status, r.processed = "dismissed", True
        else:
            r.status = "dropped"
        s.add(r)
        done.append(r.id)
    missing = wanted - set(done)
    if missing and older_than_days is None:
        s.rollback()
        raise PendingError(f"not open: {', '.join('#' + str(i) for i in sorted(missing))}")
    s.commit()
    return done


def restore(s: Session, kind: str, ids: list[int]) -> list[int]:
    """Undo a clear: the items come back as they were (a quick question is asked again, a knowledge question is
    open again). Only cleared items: an answered question stays answered."""
    if kind not in KINDS:
        raise PendingError(f"unknown kind {kind!r}")
    back = []
    for i in ids:
        if kind == "quick_question":
            r = s.get(Capture, i)
            if r is None or r.status != "dismissed" or not r.question:
                continue
            r.status, r.processed = "asking", False
        else:
            r = s.get(KnowledgeItem, i)
            if r is None or r.kind != "question" or r.status != "dropped":
                continue
            r.status = "open"
        s.add(r)
        back.append(i)
    s.commit()
    return back
