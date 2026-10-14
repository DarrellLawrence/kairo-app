"""Getting to know the owner (owner's request 2026-10-14).

Kairo keeps a list of what it doesn't know (who someone is, birthdays, family, ...).
At a good moment (never during school, work or training; max one call a day; a
declined or missed call means another day) it calls in the app for 2-3 minutes,
3-4 topics. The chat brain runs the conversation with the agenda below: smart
follow-ups, contradiction checks, stops when the owner says so. Facts go to the
vault (People, Profile); after the call one line says what was saved.

Questions can come from the model (knowledge_ask_later) and from rules here.
Nothing that asks for a password, code, PIN or bank detail is ever queued.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path
from typing import Callable

from sqlmodel import Session, select

from . import vault
from .db import Call, KnowledgeItem, Person, utcnow
from .jobkit import clip

log = logging.getLogger("kairo.knowledge")

TOPICS_PER_CALL = 4
MIN_QUESTIONS = 3          # fewer: wait, unless the oldest is OLD_DAYS old
OLD_DAYS = 3
MAX_ASKS = 3               # asked on 3 calls without an answer: dropped
LAST_CALL = "21:00"        # no getting-to-know-you call after this
BUSY_PLACES = ("school", "work", "training")
CLOSE = re.compile(r"(girl|boy)friend|partner|wife|husband|fianc|mother|father|mum|mom|dad|sister|brother|"
                   r"grand|son|daughter|best friend|mutter|vater|schwester|bruder|freundin\b|freund\b", re.I)
FAMILY = re.compile(r"mother|father|mum|mom|dad|sister|brother|grand|aunt|uncle|cousin|son|daughter|"
                    r"mutter|vater|mami|papi|schwester|bruder|oma|opa|tante|onkel|family|familie", re.I)
SECRET = re.compile(r"passw|kennwort|\bpin\b|\bcodes?\b|\btan\b|2fa|otp|iban|konto|account number|credit|kredit|"
                    r"card|karte|login|log in|anmelde|ahv|social security|bank|security question|sicherheitsfrage", re.I)
PROFILE_FILE = "10 Profile/Learned by Kairo.md"


class KnowledgeError(ValueError):
    pass


def _aware(t):
    return t if t is None or t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _clean(text: str, limit: int) -> str:
    return clip(re.sub(r"[\r\n<>`]+", " ", text or "").strip(), limit) or ""


# ---------------------------------------------------------------- the question list

def ask_later(s: Session, question: str, about: str | None = None, source: str = "chat") -> KnowledgeItem:
    q = _clean(question, 300)
    if len(q) < 3:
        raise KnowledgeError("empty question")
    if SECRET.search(q):
        raise KnowledgeError("Kairo never asks for passwords, codes, PINs or bank details")
    about = _clean(about or "", 80) or None
    for old in s.exec(select(KnowledgeItem).where(KnowledgeItem.kind == "question")
                      .where(KnowledgeItem.status.in_(["open", "answered"]))).all():
        if (old.question or "").lower() == q.lower():
            return old
    item = KnowledgeItem(question=q, about=about, source=source)
    s.add(item)
    s.commit()
    s.refresh(item)
    return item


def open_questions(s: Session) -> list[KnowledgeItem]:
    rows = s.exec(select(KnowledgeItem).where(KnowledgeItem.kind == "question").where(KnowledgeItem.status == "open")).all()
    # rules first (who someone is matters for everything else), then oldest first
    return sorted(rows, key=lambda q: (q.source != "rule", q.asks, _aware(q.created_at)))


def answered(s: Session, item_id: int, summary: str) -> KnowledgeItem:
    item = s.get(KnowledgeItem, item_id)
    if item is None or item.kind != "question":
        raise KnowledgeError(f"no question #{item_id}")
    item.status, item.answer, item.answered_at = "answered", _clean(summary, 300), utcnow()
    s.add(item)
    s.commit()
    return item


def learn_fact(s: Session, vault_dir: Path, fact: str) -> KnowledgeItem:
    """A fact about the owner: into the vault (Kairo's own section of the profile note) and the list."""
    fact = _clean(fact, 500)
    if len(fact) < 3:
        raise KnowledgeError("empty fact")
    item = KnowledgeItem(kind="fact", answer=fact, status="learned", source="chat", answered_at=utcnow())
    s.add(item)
    s.commit()
    facts = s.exec(select(KnowledgeItem).where(KnowledgeItem.kind == "fact").order_by(KnowledgeItem.id)).all()
    lines = [f"- {(_aware(f.answered_at) or utcnow()).date().isoformat()}: {f.answer}" for f in facts][-300:]
    vault.write_section(Path(vault_dir), PROFILE_FILE, "learned",
                        "## What Kairo learned about you\n(From our calls and chats. Correct anything here; Kairo reads it.)\n\n"
                        + "\n".join(lines))
    return item


def find_gaps(s: Session) -> int:
    """Rules: people without a relation, close people without a birthday, no family known."""
    n = 0
    people = s.exec(select(Person)).all()
    for p in people:
        if not p.relation:
            n += _rule(s, f"Who is {p.name} to you?", p.name)
        elif CLOSE.search(p.relation) and p.birthday is None:
            n += _rule(s, f"When is {p.name}'s birthday?", p.name)
    if people and not any(p.relation and FAMILY.search(p.relation) for p in people):
        n += _rule(s, "Who's in your close family?", "family")
    return n


def _rule(s: Session, question: str, about: str) -> int:
    exists = s.exec(select(KnowledgeItem).where(KnowledgeItem.kind == "question")
                    .where(KnowledgeItem.question == question)).first()
    if exists:
        return 0
    ask_later(s, question, about, source="rule")
    return 1


# ---------------------------------------------------------------- when to call

def _hm(text: str) -> dt.time:
    h, m = text.split(":")
    return dt.time(int(h), int(m))


def good_moment(now: dt.datetime, tz, times: dict, *, busy: Callable[[dt.datetime], bool],
                place_kind: str | None) -> tuple[bool, str]:
    local = now.astimezone(tz)
    weekend = local.weekday() >= 5
    start = _hm(times.get("weekend" if weekend else "after_work") or ("10:00" if weekend else "17:30"))
    if local.time() < start:
        return False, "not yet (school, work or morning)"
    if local.time() >= _hm(LAST_CALL):
        return False, "too late"
    if place_kind in BUSY_PLACES:
        return False, f"at {place_kind}"
    if busy(now) or busy(now + dt.timedelta(minutes=20)):
        return False, "in a calendar event"
    return True, "ok"


class Caller:
    def __init__(self, *, engine, tz, times, pusher, busy: Callable[[dt.datetime], bool],
                 place_kind: Callable[[], str | None]):
        self.engine, self.tz, self.times, self.pusher = engine, tz, times, pusher
        self.busy, self.place_kind = busy, place_kind

    def run(self, now: dt.datetime | None = None) -> dict:
        now = now or dt.datetime.now(self.tz)
        with Session(self.engine) as s:
            find_gaps(s)
            qs = open_questions(s)
            if not qs:
                return {"called": False, "why": "nothing to ask"}
            old = min(_aware(q.created_at) for q in qs) <= now - dt.timedelta(days=OLD_DAYS)
            if len(qs) < MIN_QUESTIONS and not old:
                return {"called": False, "why": f"only {len(qs)} question(s), waiting for more"}
            # Fix plan 21: one call budget (1 a day) and the owner's call windows for every call Kairo starts.
            from .voice import calls as voice_calls
            may, why = voice_calls.may_call(s, importance="normal", question=True, now=now.astimezone(self.tz))
            if not may:
                return {"called": False, "why": why}
            times = self.times() if callable(self.times) else self.times   # follows Settings changes
            ok, why = good_moment(now, self.tz, times, busy=self.busy, place_kind=self.place_kind())
            if not ok:
                return {"called": False, "why": why}
            agenda = qs[:TOPICS_PER_CALL]
            opening = (f"Hi, it's Kairo. Got two or three minutes? I'd like to get to know you a bit better. "
                       f"{agenda[0].question}")
            call = Call(reason="Getting to know you (2–3 min)", opening=opening, purpose="learn",
                        agenda_json=json.dumps([q.id for q in agenda]))
            s.add(call)
            s.commit()
            s.refresh(call)
            self.pusher.notify("Kairo is calling", call.reason, f"/#call/{call.id}", source="call")
            return {"called": True, "call_id": call.id, "topics": len(agenda)}


# ---------------------------------------------------------------- during and after the call

def agenda_items(s: Session, call: Call) -> list[KnowledgeItem]:
    ids = json.loads(call.agenda_json or "[]")
    return [q for q in (s.get(KnowledgeItem, i) for i in ids) if q is not None]


def agenda_prompt(s: Session, call: Call) -> str:
    """Kairo's own notes for the model on a getting-to-know-you call (prepended to each turn)."""
    items = agenda_items(s, call)
    todo = [q for q in items if q.status == "open"]
    done = [q for q in items if q.status == "answered"]
    people = s.exec(select(Person).order_by(Person.name)).all()
    known = "; ".join(f"{p.name} ({p.relation or 'relation unknown'}"
                      + (f", birthday {p.birthday:%d %b}" if p.birthday else "") + ")" for p in people[:60]) or "nobody yet"
    lines = [
        "[Kairo's notes for this getting-to-know-you call; not from the owner]",
        "You called the owner to get to know them better. Keep it to 2-3 minutes and the topics below; one question at a time,"
        " short and warm.",
        "Ask a smart follow-up when it matters (e.g. a partner: their birthday, your anniversary).",
        "If an answer contradicts or leaves a gap in what you know, double-check it briefly"
        " (\"So Sam isn't family, right?\"); accept corrections (\"forgot him\").",
        "Save as you go: people_note for people (relation, birthday YYYY-MM-DD, notes), profile_learn for facts about the owner,"
        " then knowledge_answered with the question id and a short summary.",
        "If the owner wants to stop (\"that's it\", \"gotta go\", \"stop\"), stop at once: one short thanks, nothing else, and end with [[hang up]].",
        "When the topics are done, say thanks in one sentence and end your reply with [[hang up]].",
        f"People you know: {known}.",
        "Still to ask: " + (" | ".join(f"#{q.id} {q.question}" for q in todo) if todo else "nothing: wrap up now."),
    ]
    if done:
        lines.append("Already answered on this call: " + " | ".join(f"#{q.id} {q.answer}" for q in done))
    return "\n".join(lines)


def finish(s: Session, call: Call) -> str | None:
    """After hang-up: unanswered topics come back another day (dropped after MAX_ASKS); one line to the owner."""
    since = _aware(call.answered_at) or _aware(call.created_at)
    saved = []
    for q in agenda_items(s, call):
        if q.status == "answered" and q.answer:
            saved.append(q.answer)
        elif q.status == "open":
            q.asks += 1
            if q.asks >= MAX_ASKS:
                q.status = "dropped"
            s.add(q)
    for f in s.exec(select(KnowledgeItem).where(KnowledgeItem.kind == "fact")).all():
        if f.answered_at and _aware(f.answered_at) >= since and f.answer not in saved:
            saved.append(f.answer)
    s.commit()
    if not saved:
        return None
    return "Saved from our call: " + "; ".join(x.rstrip(".") for x in saved) + "."
