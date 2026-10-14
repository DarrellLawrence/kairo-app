"""School (Milestone D): test dates, grades, study guides, flashcards.

Grades use the Swiss scale (1-6, 4 = pass). Averages are weighted; the
report value is rounded to the nearest half grade. "What do I need" says
which grade the next test needs for a target average.

Study guides and flashcards are written by the model from the owner's own
material (pasted text and the vault's 40 School/<subject>/ notes), passed in
as untrusted data. Cards are learned with Leitner boxes:
right -> next box (1, 2, 4, 8, 16 days), wrong -> box 1, again today.
"""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from . import vault
from .db import Flashcard, Grade, SchoolEvent
from .jobkit import JOB_RULES, clip, extract_json
from .tools import untrusted

INTERVALS = {1: 1, 2: 2, 3: 4, 4: 8, 5: 16}
MAX_MATERIAL = 30_000


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


SUBJECT = Field(min_length=1, max_length=60)


class EventIn(_In):
    subject: str = SUBJECT
    kind: Literal["test", "deadline", "lesson"] = "test"
    date: dt.datetime
    notes: str | None = Field(default=None, max_length=2000)


class GradeIn(_In):
    subject: str = SUBJECT
    grade: float = Field(ge=1, le=6)
    weight: float = Field(default=1.0, gt=0, le=10)
    date: dt.date | None = None
    title: str | None = Field(default=None, max_length=100)


class MaterialIn(_In):
    subject: str = SUBJECT
    topic: str = Field(min_length=1, max_length=120)
    material: str = Field(default="", max_length=MAX_MATERIAL)
    n: int = Field(default=15, ge=3, le=40)


class CardIn(_In):
    subject: str = SUBJECT
    topic: str | None = Field(default=None, max_length=120)
    front: str = Field(min_length=1, max_length=1000)
    back: str = Field(min_length=1, max_length=2000)


class SchoolError(ValueError):
    pass


# ---------------------------------------------------------------- grades

def averages(grades: list[Grade]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for g in grades:
        d = out.setdefault(g.subject, {"sum": 0.0, "w": 0.0, "n": 0})
        d["sum"] += g.grade * g.weight
        d["w"] += g.weight
        d["n"] += 1
    return {k: {"average": round(v["sum"] / v["w"], 2), "report": half(v["sum"] / v["w"]), "count": v["n"], "weight": v["w"]}
            for k, v in out.items()}


def half(x: float) -> float:
    """Round to the nearest half grade (4.25 -> 4.5, 4.24 -> 4.0)."""
    return math.floor(x * 2 + 0.5) / 2


def needed(grades: list[Grade], target: float, weight: float = 1.0) -> float | None:
    """Grade the next test (with `weight`) needs so the average reaches `target`. None = no grades yet."""
    if not grades:
        return None
    total = sum(g.grade * g.weight for g in grades)
    w = sum(g.weight for g in grades)
    return round((target * (w + weight) - total) / weight, 2)


# ---------------------------------------------------------------- flashcards

def review(card: Flashcard, knew: bool, today: dt.date) -> Flashcard:
    from .db import utcnow
    card.reviewed_at = utcnow()
    if knew:
        card.box = min(5, card.box + 1)
        card.due = today + dt.timedelta(days=INTERVALS[card.box])
    else:
        card.box, card.due = 1, today
    return card


def due_cards(s: Session, today: dt.date, subject: str | None = None, limit: int = 50) -> list[Flashcard]:
    q = select(Flashcard).where(Flashcard.due <= today).order_by(Flashcard.box, Flashcard.id).limit(limit)
    if subject:
        q = q.where(Flashcard.subject == subject)
    return list(s.exec(q).all())


def card_dict(c: Flashcard) -> dict:
    return {"id": c.id, "subject": c.subject, "topic": c.topic, "front": c.front, "back": c.back, "box": c.box,
            "due": c.due.isoformat()}


# ---------------------------------------------------------------- model work

def _notes(vault_dir: Path, subject: str, limit: int = 15_000) -> str:
    folder = Path(vault_dir) / "40 School" / subject
    if not folder.is_dir():
        return ""
    parts, used = [], 0
    for p in sorted(folder.rglob("*.md")):
        if vault.is_conflict(p):
            continue   # a Syncthing conflict copy (fix plan 33)
        text = p.read_text(encoding="utf-8", errors="replace")
        if "<!-- kairo:study-guide -->" in text:
            continue   # don't feed Kairo's own guides back in
        chunk = f"## {p.name}\n{text}\n"
        parts.append(chunk[: max(0, limit - used)])
        used += len(chunk)
        if used >= limit:
            break
    return "\n".join(parts)


GUIDE_PROMPT = """Write a study guide for the owner's {subject} test on "{topic}".
Use only the material below (the owner's notes and what they pasted); if something
important is missing, say what to look up instead of inventing it.
Return JSON: {{"title": "...", "markdown": "the guide in Markdown: key ideas, definitions,
formulas, typical tasks with one worked example, common mistakes, a 10-minute plan"}}
Write in the language of the material.

{material}"""

CARDS_PROMPT = """Make {n} flashcards for the owner's {subject} test on "{topic}" from the material below.
One fact, term, formula or question per card; short. Only what the material supports.
Return JSON: [{{"front": "question", "back": "answer"}}, ...]
Write in the language of the material.

{material}"""


def _material(vault_dir: Path, data: MaterialIn) -> str:
    notes = _notes(vault_dir, data.subject)
    if not notes and not data.material.strip():
        raise SchoolError(f"No material: paste some notes, or put notes in the vault under 40 School/{data.subject}/")
    out = []
    if data.material.strip():
        out.append(untrusted("material the owner pasted", data.material))
    if notes:
        out.append(untrusted(f"the owner's vault notes for {data.subject}", notes))
    return "\n\n".join(out)


def make_guide(ask, vault_dir: Path, data: MaterialIn) -> dict:
    answer = ask(GUIDE_PROMPT.format(subject=data.subject, topic=data.topic, material=_material(vault_dir, data)), JOB_RULES)
    if not answer.ok:
        raise SchoolError(f"Claude could not do it: {answer.error or answer.status}")
    out = extract_json(answer.text)
    if not isinstance(out, dict) or not isinstance(out.get("markdown"), str):
        raise SchoolError("Claude's answer was not usable; try again")
    rel = f"40 School/{_safe(data.subject)}/{_safe(data.topic)} - study guide.md"
    vault.write_section(vault_dir, rel, "study-guide", out["markdown"].strip())
    return {"title": clip(out.get("title") or data.topic, 160), "markdown": out["markdown"], "vault_path": rel}


def make_cards(ask, s: Session, vault_dir: Path, data: MaterialIn, today: dt.date) -> list[Flashcard]:
    answer = ask(CARDS_PROMPT.format(n=data.n, subject=data.subject, topic=data.topic, material=_material(vault_dir, data)), JOB_RULES)
    if not answer.ok:
        raise SchoolError(f"Claude could not do it: {answer.error or answer.status}")
    rows = extract_json(answer.text)
    if not isinstance(rows, list):
        raise SchoolError("Claude's answer was not usable; try again")
    cards = []
    for r in rows[: data.n]:
        if isinstance(r, dict) and str(r.get("front") or "").strip() and str(r.get("back") or "").strip():
            c = Flashcard(subject=data.subject, topic=data.topic, front=str(r["front"]).strip()[:1000],
                          back=str(r["back"]).strip()[:2000], box=1, due=today)
            s.add(c)
            cards.append(c)
    if not cards:
        raise SchoolError("No cards came back; try again with more material")
    s.commit()
    for c in cards:
        s.refresh(c)
    return cards


def _safe(name: str) -> str:
    import re
    return " ".join(re.sub(r"[\\/:*?\"<>|#^\[\]]+", " ", name).split())[:80] or "Notes"


# ---------------------------------------------------------------- overview

def overview(s: Session, today: dt.date, tz) -> dict:
    start = dt.datetime.combine(today, dt.time.min, tzinfo=tz)
    events = s.exec(select(SchoolEvent).where(SchoolEvent.date >= start.astimezone(dt.timezone.utc)).order_by(SchoolEvent.date)).all()
    grades = s.exec(select(Grade).order_by(Grade.date.desc(), Grade.id.desc())).all()
    cards = s.exec(select(Flashcard)).all()
    subjects = sorted({g.subject for g in grades} | {e.subject for e in events} | {c.subject for c in cards})
    due = {}
    for c in cards:
        if c.due <= today:
            due[c.subject] = due.get(c.subject, 0) + 1
    return {
        "events": [{"id": e.id, "subject": e.subject, "kind": e.kind, "date": e.date.isoformat(), "notes": e.notes} for e in events],
        "grades": [{"id": g.id, "subject": g.subject, "grade": g.grade, "weight": g.weight, "date": g.date.isoformat(),
                    "title": g.title} for g in grades],
        "averages": averages(list(grades)),
        "subjects": subjects,
        "cards": {sub: {"total": sum(1 for c in cards if c.subject == sub), "due": due.get(sub, 0)} for sub in subjects},
    }
