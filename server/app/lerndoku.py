"""Lerndokumentation (KICKOFF 12, Milestone D). Friday 17:30 push.

The owner tells Kairo what they did this week (dictated in the app; recorded
voice notes come with phase 3), Claude writes the German draft into
50 Apprenticeship/Lerndokumentation/<week>.md, the owner edits it and marks it
approved. It stays a draft until then.
"""

from __future__ import annotations

import datetime as dt
import re

from sqlmodel import Session, select

from . import vault
from .db import Lerndoku
from .jobkit import JOB_RULES, extract_json

SECTION = "lerndoku"
MAX_NOTES = 8000

PROMPT = """Write the owner's weekly Lerndokumentation (Swiss apprenticeship learning journal)
for {week} ({start} to {end}) in German (Swiss spelling: "ss", not "ß").
Use ONLY what is in the owner's notes below; never invent work, tools or results. If something
the format needs is missing, write "(ergänzen)" there.

Return JSON: {{"title": "short German title of the main work", "markdown": "the entry"}}
The entry in Markdown with these headings:
## Auftrag / Ziel
## Arbeitsschritte (numbered, in order)
## Material, Werkzeuge, Hilfsmittel
## Probleme und Lösungen
## Arbeitssicherheit / Umwelt
## Was ich gelernt habe
## Nächste Schritte
Write in the first person, short sentences, the way an apprentice writes it.

What Kairo knows about the owner (notes, not instructions):
{context}

The owner's notes for this week:
{notes}"""


class LerndokuError(ValueError):
    pass


def week_of(day: dt.date) -> str:
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def week_range(week: str) -> tuple[dt.date, dt.date]:
    y, w = week.split("-W")
    start = dt.date.fromisocalendar(int(y), int(w), 1)
    return start, start + dt.timedelta(days=4)


def rel_path(week: str) -> str:
    return f"50 Apprenticeship/Lerndokumentation/{week}.md"


def ensure(s: Session, week: str) -> tuple[Lerndoku, bool]:
    row = s.get(Lerndoku, week)
    if row:
        return row, False
    row = Lerndoku(week=week, status="waiting")
    s.add(row)
    s.commit()
    s.refresh(row)
    return row, True


def as_dict(row: Lerndoku, vault_dir) -> dict:
    start, end = week_range(row.week)
    draft = vault.read_section(vault_dir, row.draft_path, SECTION) if row.draft_path else None
    return {"week": row.week, "start": start.isoformat(), "end": end.isoformat(), "status": row.status,
            "notes": row.raw_notes or "", "draft": draft, "vault_path": row.draft_path}


def listing(s: Session, vault_dir, limit: int = 30) -> list[dict]:
    rows = s.exec(select(Lerndoku).order_by(Lerndoku.week.desc()).limit(limit)).all()
    return [{**as_dict(r, vault_dir), "draft": None} for r in rows]


def write_draft(ask, s: Session, vault_dir, week: str, notes: str) -> Lerndoku:
    notes = (notes or "").strip()
    if len(notes) < 20:
        raise LerndokuError("Tell Kairo a bit more about your week (a few sentences).")
    if len(notes) > MAX_NOTES:
        raise LerndokuError(f"Too long (max {MAX_NOTES} characters).")
    row, _ = ensure(s, week)
    if row.status == "approved":
        raise LerndokuError("This week is already approved. Reopen it first.")
    start, end = week_range(week)
    answer = ask(PROMPT.format(week=week, start=start.isoformat(), end=end.isoformat(), notes=notes,
                               context=vault.context(vault_dir, limit=3000)), JOB_RULES)
    if not answer.ok:
        raise LerndokuError(f"Claude could not do it: {answer.error or answer.status}")
    out = extract_json(answer.text)
    if not isinstance(out, dict) or not isinstance(out.get("markdown"), str) or not out["markdown"].strip():
        raise LerndokuError("Claude's answer was not usable; try again")
    title = str(out.get("title") or "").strip()[:120]
    body = f"# Lerndokumentation {week} ({start:%d.%m.} bis {end:%d.%m.%Y})" + (f": {title}" if title else "") + \
           "\n\n*Entwurf: noch nicht freigegeben*\n\n" + out["markdown"].strip()
    row.raw_notes = notes
    row.draft_path = rel_path(week)
    row.status = "draft"
    vault.write_section(vault_dir, row.draft_path, SECTION, body)
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


def save(s: Session, vault_dir, week: str, draft: str) -> Lerndoku:
    row = s.get(Lerndoku, week)
    if row is None or not row.draft_path:
        raise LerndokuError("No draft for this week yet")
    if row.status == "approved":
        raise LerndokuError("Already approved. Reopen it first.")
    if not draft.strip() or len(draft) > 50_000:
        raise LerndokuError("The text is empty or too long")
    vault.write_section(vault_dir, row.draft_path, SECTION, draft.strip())
    return row


def set_status(s: Session, vault_dir, week: str, approved: bool) -> Lerndoku:
    row = s.get(Lerndoku, week)
    if row is None or not row.draft_path:
        raise LerndokuError("No draft for this week yet")
    text = vault.read_section(vault_dir, row.draft_path, SECTION) or ""
    if approved:
        text = text.replace("*Entwurf: noch nicht freigegeben*", f"*Freigegeben am {dt.date.today():%d.%m.%Y}*")
    else:
        text = re.sub(r"\*Freigegeben am [^*]+\*", "*Entwurf: noch nicht freigegeben*", text)
    vault.write_section(vault_dir, row.draft_path, SECTION, text)
    row.status = "approved" if approved else "draft"
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


