"""Reviews (v5 Milestone M): daily check-in up to the yearly review.

Each review is created on its day (scheduler.py) with data Kairo already has
(pre-filled), the owner answers a few questions in the app, and the result is
saved to the database and as a note in `90 Reviews/`. Commitments ("top 3 for
next week") become tasks.

Schedule (owner's timezone):
  daily      every evening (21:00)
  weekly     Sunday (18:00)
  monthly    last Sunday of the month
  quarterly  last Sunday of March, June, September, December
  half_year  last Sunday of June and December
  yearly     last Sunday of December
When several fall on the same Sunday, only the biggest is created (the yearly
review covers the month, the quarter and the half-year).
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
from dataclasses import asdict, dataclass

from sqlmodel import Session, select

from . import life, location, vault
from .db import Approval, Checkin, OnboardingAnswer, Review, Task, utcnow

BIG = ["weekly", "monthly", "quarterly", "half_year", "yearly"]   # smallest -> biggest
TITLES = {"daily": "Evening check-in", "weekly": "Weekly review", "monthly": "Monthly review",
          "quarterly": "Quarterly review", "half_year": "Half-year review", "yearly": "Yearly review"}
MINUTES = {"daily": 2, "weekly": 5, "monthly": 8, "quarterly": 10, "half_year": 12, "yearly": 15}
MAX_ANSWER = 4000


@dataclass(frozen=True)
class Q:
    id: str
    text: str
    kind: str = "text"        # text | lines | scale (1-5)
    commit: bool = False      # lines become tasks


QUESTIONS: dict[str, list[Q]] = {
    "daily": [
        Q("energy", "Energy today?", "scale"),
        Q("done", "What got done today?"),
        Q("tomorrow", "The one thing for tomorrow?", "lines", commit=True),
    ],
    "weekly": [
        Q("wins", "Wins this week?"),
        Q("missed", "What didn't happen, and why?"),
        Q("next", "Top 3 for next week", "lines", commit=True),
        Q("energy", "Energy this week?", "scale"),
    ],
    "monthly": [
        Q("progress", "Biggest progress this month?"),
        Q("stop_start_keep", "Stop / start / keep?"),
        Q("goals", "Goals: on track? What needs attention?"),
        Q("next", "Focus for next month (up to 3)", "lines", commit=True),
    ],
    "quarterly": [
        Q("progress", "What moved forward this quarter?"),
        Q("goals", "Which goals change, drop or get added?"),
        Q("lesson", "Biggest lesson?"),
        Q("next", "Top 3 for next quarter", "lines", commit=True),
    ],
    "half_year": [
        Q("progress", "Looking back at the half-year: what are you proud of?"),
        Q("goals", "Where are you against the year's goals?"),
        Q("change", "What should change for the next six months?"),
        Q("next", "Top 3 for the next half-year", "lines", commit=True),
    ],
    "yearly": [
        Q("proud", "What are you proudest of this year?"),
        Q("lesson", "What did the year teach you?"),
        Q("stop_start_keep", "Stop / start / keep for next year?"),
        Q("next", "Your 3 goals for next year", "lines", commit=True),
    ],
}


class ReviewError(Exception):
    pass


# ---------------------------------------------------------------- calendar


def _last_sunday(year: int, month: int) -> dt.date:
    last = dt.date(year, month, calendar.monthrange(year, month)[1])
    return last - dt.timedelta(days=(last.weekday() - 6) % 7)


def big_kind_due(day: dt.date) -> str | None:
    """The biggest review due on this day (Sundays only), or None."""
    if day.weekday() != 6:
        return None
    if day != _last_sunday(day.year, day.month):
        return "weekly"
    if day.month == 12:
        return "yearly"
    if day.month == 6:
        return "half_year"
    if day.month in (3, 9):
        return "quarterly"
    return "monthly"


def period(kind: str, day: dt.date) -> tuple[dt.date, dt.date]:
    if kind == "daily":
        return day, day
    if kind == "weekly":
        return day - dt.timedelta(days=day.weekday()), day
    if kind == "monthly":
        return day.replace(day=1), day
    if kind == "quarterly":
        return dt.date(day.year, 3 * ((day.month - 1) // 3) + 1, 1), day
    if kind == "half_year":
        return dt.date(day.year, 1 if day.month <= 6 else 7, 1), day
    if kind == "yearly":
        return dt.date(day.year, 1, 1), day
    raise ReviewError(f"unknown review kind {kind}")


# ---------------------------------------------------------------- data


def _between(col, start: dt.date, end: dt.date):
    a = dt.datetime.combine(start, dt.time.min, tzinfo=dt.timezone.utc)
    b = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tzinfo=dt.timezone.utc)
    return (col >= a) & (col < b)


def prefill(session: Session, kind: str, start: dt.date, end: dt.date, tz=None) -> dict:
    """What Kairo already knows about the period, shown above the questions."""
    done = session.exec(select(Task).where(Task.status == "done").where(_between(Task.done_at, start, end))).all()
    open_tasks = session.exec(select(Task).where(Task.status.in_(["open", "doing"]))).all()
    overdue = [t for t in open_tasks if t.due and t.due.date() < end]
    sent = session.exec(select(Approval).where(Approval.status == "done").where(_between(Approval.decided_at, start, end))).all()
    checkins = session.exec(select(Checkin).where(Checkin.date >= start).where(Checkin.date <= end)).all()
    energies = [c.energy_1_5 for c in checkins if c.energy_1_5]
    prev = session.exec(select(Review).where(Review.kind == kind).where(Review.status == "done")
                        .where(Review.period_start < start).order_by(Review.period_start.desc())).first()
    goals = {a.question_id: a.value for a in session.exec(select(OnboardingAnswer)).all() if a.question_id.startswith("goals_")}
    facts = []
    if kind != "daily":
        facts.append(f"{len(done)} tasks done" + (f": {', '.join(t.title for t in done[:8])}" if done else ""))
        if energies:
            facts.append(f"Average energy {sum(energies) / len(energies):.1f}/5 over {len(energies)} check-ins")
        facts.append(f"{len(sent)} approved emails/actions")
    else:
        facts.append(f"{len(done)} tasks done today" + (f": {', '.join(t.title for t in done[:5])}" if done else ""))
    if overdue:
        facts.append(f"{len(overdue)} overdue: {', '.join(t.title for t in overdue[:5])}")
    if open_tasks:
        facts.append(f"{len(open_tasks)} open tasks")
    if kind != "daily":
        h = life.health_summary(session, (end - start).days + 1, end)["avg"]
        if h["sleep_h"] is not None:
            facts.append(f"Sleep {h['sleep_h']} h on average" + (f", {int(h['steps'])} steps a day" if h["steps"] else ""))
        from .db import OpenLoop
        pats = [l.what for l in session.exec(select(OpenLoop).where(OpenLoop.kind == "pattern")).all()]
        if pats:
            facts.append("Patterns Kairo noticed: " + " | ".join(pats[:4]))
        if kind != "weekly":
            m = life.money_summary(session, (end - start).days + 1,
                                   now=dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tzinfo=dt.timezone.utc))
            if m["spent_chf"]:
                top = ", ".join(f"{k} {v:.0f}" for k, v in list(m["by_category"].items())[:3])
                facts.append(f"Spent CHF {m['spent_chf']:.0f} ({top})")
        facts += location.patterns(session, tz or dt.timezone.utc, weeks=max(1, (end - start).days // 7 + 1), today=end)
    return {
        "facts": facts,
        "last_commitments": json.loads(prev.commitments_json) if prev and prev.commitments_json else [],
        "goals": goals if kind in ("monthly", "quarterly", "half_year", "yearly") else {},
    }


# ---------------------------------------------------------------- lifecycle


def create(session: Session, kind: str, day: dt.date, tz=None) -> tuple[Review, bool]:
    """Idempotent: one review per kind and period. Returns (review, created)."""
    if kind not in QUESTIONS:
        raise ReviewError(f"unknown review kind {kind}")
    start, end = period(kind, day)
    existing = session.exec(select(Review).where(Review.kind == kind).where(Review.period_start == start)).first()
    if existing:
        return existing, False
    r = Review(kind=kind, period_start=start, period_end=end, status="open",
               prefill_json=json.dumps(prefill(session, kind, start, end, tz)))
    session.add(r)
    session.commit()
    session.refresh(r)
    return r, True


def as_dict(r: Review) -> dict:
    return {"id": r.id, "kind": r.kind, "title": TITLES[r.kind], "minutes": MINUTES[r.kind],
            "period_start": r.period_start.isoformat(), "period_end": (r.period_end or r.period_start).isoformat(),
            "status": r.status, "prefill": json.loads(r.prefill_json or "{}"),
            "answers": json.loads(r.answers_json or "{}"), "questions": [asdict(q) for q in QUESTIONS[r.kind]],
            "vault_path": r.vault_path}


def complete(session: Session, vault_dir, review_id: int, given: dict) -> Review:
    r = session.get(Review, review_id)
    if r is None:
        raise ReviewError("no such review")
    if r.status != "open":
        raise ReviewError("this review is already closed")
    qs = {q.id: q for q in QUESTIONS[r.kind]}
    unknown = set(given) - set(qs)
    if unknown:
        raise ReviewError(f"unknown answers: {', '.join(sorted(unknown))}")
    clean: dict[str, object] = {}
    for qid, q in qs.items():
        v = given.get(qid)
        if v in (None, ""):
            continue
        if q.kind == "scale":
            if not isinstance(v, int) or not 1 <= v <= 5:
                raise ReviewError(f"{qid}: pick 1 to 5")
            clean[qid] = v
        else:
            v = str(v).replace("\r\n", "\n").strip()
            if len(v) > MAX_ANSWER:
                raise ReviewError(f"{qid}: answer longer than {MAX_ANSWER} characters")
            clean[qid] = v
    commitments = [line.strip("-• ").strip() for q in qs.values() if q.commit
                   for line in str(clean.get(q.id, "")).split("\n") if line.strip("-• ").strip()][:5]

    r.answers_json = json.dumps(clean)
    r.commitments_json = json.dumps(commitments)
    r.status, r.completed_at = "done", utcnow()
    for c in commitments:
        session.add(Task(title=c[:200], source="review", priority=1))
    if r.kind == "daily":
        row = session.get(Checkin, r.period_start) or Checkin(date=r.period_start)
        row.energy_1_5 = clean.get("energy")
        row.done_text = clean.get("done")
        row.top3_json = json.dumps(commitments)
        session.add(row)
    rel = f"90 Reviews/{TITLES[r.kind]}s/{r.period_start.isoformat()}.md"
    vault.write_section(vault_dir, rel, "review", _note(r, clean))
    r.vault_path = rel
    session.add(r)
    session.commit()
    session.refresh(r)
    return r


def skip(session: Session, review_id: int) -> Review:
    r = session.get(Review, review_id)
    if r is None or r.status != "open":
        raise ReviewError("no open review with that id")
    r.status = "skipped"
    session.add(r)
    session.commit()
    session.refresh(r)
    return r


def _note(r: Review, answers: dict) -> str:
    pre = json.loads(r.prefill_json or "{}")
    end = r.period_end or r.period_start
    out = [f"# {TITLES[r.kind]}: {r.period_start.isoformat()}" + ("" if end == r.period_start else f" to {end.isoformat()}"), ""]
    if pre.get("facts"):
        out += ["## What happened (from Kairo)", *[f"- {f}" for f in pre["facts"]], ""]
    if pre.get("last_commitments"):
        out += ["## Last time you planned", *[f"- {c}" for c in pre["last_commitments"]], ""]
    out.append("## Your answers")
    for q in QUESTIONS[r.kind]:
        v = answers.get(q.id)
        if v in (None, ""):
            continue
        out.append(f"**{q.text}**")
        if q.kind == "lines":
            out += [f"- {line.strip()}" for line in str(v).split("\n") if line.strip()]
        elif q.kind == "scale":
            out.append(f"{v}/5")
        else:
            out.append(str(v))
        out.append("")
    return "\n".join(out)
