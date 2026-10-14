"""Capture pipeline (KICKOFF 13, Milestone C).

In:   iPhone Shortcut (Action button / share sheet) -> POST /api/capture with
      the capture token; the app's own capture box. Text and links as JSON,
      photos/PDFs/audio as the raw request body (no extra library needed).
Store: <data>/inbox/<date>/ + a captures row. Answer fast (< 5 s).
Then:  every 15 minutes (only if something is waiting) the model classifies
      each text capture; the backend files it: task, school event, note,
      person note, idea. Unclear -> Kairo asks (push), never guesses.
Photos, PDFs and audio wait for a look ("later") until reading them lands.

Captures are untrusted data: they can become tasks and notes (tier 0-1),
never approval cards.
"""

from __future__ import annotations

import datetime as dt
import hmac
import json
import re
import secrets
from pathlib import Path

from sqlmodel import Session, select

from . import vault
from .db import Capture, SchoolEvent, Task, utcnow
from .jobkit import JOB_RULES, clip, extract_json
from .scheduler import JobContext, Skipped
from .tools import untrusted

TOKEN_FILE = "capture-token.txt"
MAX_TEXT = 20_000
MAX_FILE = 25 * 1024 * 1024
FILE_KINDS = {
    "image/jpeg": ("photo", ".jpg"), "image/png": ("photo", ".png"), "image/heic": ("photo", ".heic"),
    "image/heif": ("photo", ".heic"), "application/pdf": ("pdf", ".pdf"), "audio/mp4": ("audio", ".m4a"),
    "audio/x-m4a": ("audio", ".m4a"), "audio/mpeg": ("audio", ".mp3"), "audio/wav": ("audio", ".wav"),
}
BATCH = 10

PROMPT = """The owner captured these items quickly on the phone. For each one decide what it is
and return a JSON array, one object per item:
{{"id": <item id>,
  "type": "task" | "school_event" | "note" | "person" | "idea" | "question" | "unclear",
  "title": "short title in the owner's words (keep their language)",
  "due": "YYYY-MM-DD" or null            (tasks: only if a date is given or obvious),
  "subject": "school subject" or null    (school_event),
  "event_kind": "test" | "deadline" | "lesson" or null (school_event),
  "date": "YYYY-MM-DD" or null           (school_event),
  "person": "name" or null               (person),
  "note": "the content to keep, cleaned up, nothing invented",
  "ask": "one short question to the owner" or null (only for unclear)}}
Use "unclear" whenever you would have to guess. Today is {today}.
Earlier answers from the owner are included where Kairo asked before.

{items}"""


# ---------------------------------------------------------------- token

def token(secrets_dir: Path) -> str:
    """The Shortcut's capture token (made on first use, kept in data/secrets)."""
    path = Path(secrets_dir) / TOKEN_FILE
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(24), encoding="ascii")
    return path.read_text(encoding="ascii").strip()


def check_token(secrets_dir: Path, header: str | None) -> bool:
    sent = (header or "").removeprefix("Bearer ").strip()
    return bool(sent) and hmac.compare_digest(sent, token(secrets_dir))


def rotate_token(secrets_dir: Path) -> str:
    (Path(secrets_dir) / TOKEN_FILE).unlink(missing_ok=True)
    return token(secrets_dir)


# ---------------------------------------------------------------- store

class CaptureError(ValueError):
    pass


def _day_dir(inbox: Path, profile) -> Path:
    d = Path(inbox) / dt.datetime.now(profile.tz).date().isoformat()
    d.mkdir(parents=True, exist_ok=True)
    return d


def store_text(s: Session, inbox: Path, profile, *, text: str | None, url: str | None, source: str) -> Capture:
    text = (text or "").strip()
    url = (url or "").strip()
    if not text and not url:
        raise CaptureError("nothing to capture")
    if url and not re.match(r"^https?://\S+$", url):
        raise CaptureError("link must start with http(s)://")
    body = (text + ("\n" + url if url and url not in text else "")).strip()
    if len(body) > MAX_TEXT:
        raise CaptureError(f"too long (max {MAX_TEXT} characters)")
    cap = Capture(kind="link" if url and not text else "text", text=body, source=source)
    s.add(cap)
    s.commit()
    s.refresh(cap)
    path = _day_dir(inbox, profile) / f"{cap.id:06d}.txt"
    path.write_text(body, encoding="utf-8")
    cap.raw_path = str(path)
    s.add(cap)
    s.commit()
    s.refresh(cap)
    return cap


def store_file(s: Session, inbox: Path, profile, *, data: bytes, content_type: str, note: str | None, source: str) -> Capture:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype not in FILE_KINDS:
        raise CaptureError(f"can't keep {ctype or 'this file type'} (photos, PDFs and voice memos only)")
    if not data:
        raise CaptureError("empty file")
    if len(data) > MAX_FILE:
        raise CaptureError("file too big (max 25 MB)")
    kind, ext = FILE_KINDS[ctype]
    cap = Capture(kind=kind, text=(note or "").strip()[:2000] or None, source=source, status="later")
    s.add(cap)
    s.commit()
    s.refresh(cap)
    path = _day_dir(inbox, profile) / f"{cap.id:06d}{ext}"
    path.write_bytes(data)
    cap.raw_path = str(path)
    s.add(cap)
    s.commit()
    s.refresh(cap)
    return cap


def as_dict(c: Capture) -> dict:
    return {"id": c.id, "kind": c.kind, "text": c.text, "source": c.source, "status": c.status, "filed_as": c.filed_as,
            "title": c.title, "question": c.question, "created_at": c.created_at, "filed_at": c.filed_at,
            "result": json.loads(c.result_json) if c.result_json else None}


def answer(s: Session, capture_id: int, text: str) -> Capture:
    """The owner answered Kairo's question: process the capture again with the answer."""
    cap = s.get(Capture, capture_id)
    if cap is None or cap.status != "asking":
        raise CaptureError("no question open for this capture")
    text = (text or "").strip()
    if not text:
        raise CaptureError("empty answer")
    cap.text = f"{cap.text or ''}\n[Kairo asked: {cap.question}]\n[Owner answered: {text[:1000]}]"
    cap.status, cap.question = "new", None
    s.add(cap)
    s.commit()
    s.refresh(cap)
    return cap


def dismiss(s: Session, capture_id: int) -> Capture:
    cap = s.get(Capture, capture_id)
    if cap is None:
        raise CaptureError("no such capture")
    cap.status, cap.processed = "dismissed", True
    s.add(cap)
    s.commit()
    s.refresh(cap)
    return cap


# ---------------------------------------------------------------- process

class Processor:
    def __init__(self, *, engine, vault_dir: Path, profile, on_question=None):
        self.engine = engine
        self.vault_dir = Path(vault_dir)
        self.profile = profile
        self.on_question = on_question

    def waiting(self, s: Session) -> list[Capture]:
        return list(s.exec(select(Capture).where(Capture.status == "new").order_by(Capture.id).limit(BATCH)).all())

    def run(self, ctx: JobContext) -> str:
        s = ctx.session
        caps = self.waiting(s)
        if not caps:
            raise Skipped("inbox empty")
        today = dt.datetime.now(self.profile.tz).date()
        items = "\n\n".join(untrusted(f"capture (id {c.id}, {c.kind})", c.text or "") for c in caps)
        answer_ = ctx.ask(PROMPT.format(today=today.isoformat(), items=items), JOB_RULES)
        if not answer_.ok:
            raise RuntimeError(f"model failed: {answer_.error or answer_.status}")
        rows = extract_json(answer_.text)
        if not isinstance(rows, list):
            raise ValueError("capture answer is not a list")
        by_id = {c.id: c for c in caps}
        done = {"filed": 0, "asking": 0}
        for row in rows:
            if not isinstance(row, dict):
                continue
            cap = by_id.pop(_int(row.get("id")), None)
            if cap is None:
                continue
            status = self.file(s, cap, row, today)
            done["filed" if status == "filed" else "asking"] += 1
        return json.dumps(done)

    def file(self, s: Session, cap: Capture, row: dict, today: dt.date) -> str:
        kind = row.get("type")
        title = clip(row.get("title") or cap.text, 160)
        note = (row.get("note") or cap.text or "").strip()[:10_000]
        result: dict = {"type": kind}
        tz = self.profile.tz
        if kind == "task":
            due = _day(row.get("due"))
            t = Task(title=title, due=dt.datetime.combine(due, dt.time(12), tzinfo=tz) if due else None,
                     priority=1, source="capture")
            s.add(t)
            s.commit()
            s.refresh(t)
            result["task_id"] = t.id
        elif kind == "school_event" and _day(row.get("date")):
            ev = SchoolEvent(subject=clip(row.get("subject") or "School", 80),
                             kind=row.get("event_kind") if row.get("event_kind") in ("test", "deadline", "lesson") else "deadline",
                             date=dt.datetime.combine(_day(row.get("date")), dt.time(8), tzinfo=tz), notes=note or None)
            s.add(ev)
            s.commit()
            s.refresh(ev)
            result["school_event_id"] = ev.id
        elif kind in ("note", "idea", "person", "question"):
            folder = {"note": "00 Inbox", "idea": "Ideas", "person": "60 People", "question": "00 Inbox"}[kind]
            name = _filename(row.get("person") if kind == "person" else title) or f"Capture {cap.id}"
            rel = f"{folder}/{name}.md"
            stamp = dt.datetime.now(tz).strftime("%Y-%m-%d %H:%M")
            existing = vault.read_section(self.vault_dir, rel, f"capture-{cap.id}")
            if existing is None:
                vault.write_section(self.vault_dir, rel, f"capture-{cap.id}", f"*{stamp} (captured)*\n\n{note}")
            result["vault_path"] = rel
        else:
            cap.status = "asking"
            cap.question = clip(row.get("ask") or "What should I do with this?", 300)
            cap.title = title
            s.add(cap)
            s.commit()
            if self.on_question:
                self.on_question(cap)
            return "asking"
        cap.status, cap.processed, cap.filed_as, cap.title = "filed", True, kind, title
        cap.result_json, cap.filed_at = json.dumps(result), utcnow()
        s.add(cap)
        s.commit()
        return "filed"


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _day(v) -> dt.date | None:
    try:
        return dt.date.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None


def _filename(v) -> str:
    name = re.sub(r"[\\/:*?\"<>|#^\[\]]+", " ", str(v or "")).strip()
    return " ".join(name.split())[:80]
