"""Onboarding interview (v5 Milestone M): about 20 minutes of questions, one
at a time in the app. Answers live in the database (the source of truth) and
are rendered into Kairo-owned sections of vault files, so Kairo knows them in
every chat. Redo or edit any answer at any time.

The questions are fixed here (no model involved): what the owner answers is
stored exactly as typed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlmodel import Session, select

from . import vault
from .db import OnboardingAnswer, utcnow

MAX_ANSWER = 4000


@dataclass(frozen=True)
class Question:
    id: str
    section: str
    text: str
    hint: str = ""
    kind: str = "text"        # text | lines (one item per line)
    file: str = "10 Profile/Profile.md"


Q = Question
QUESTIONS: list[Question] = [
    # about you
    Q("about", "You", "Tell me about yourself in a few sentences.", "Age, where you live, what fills your week."),
    Q("strengths", "You", "What are you good at, and what do you want to get better at?"),
    Q("values", "You", "What matters most to you right now?", "e.g. health, money, school, family, freedom"),
    # goals per area
    Q("goals_year", "Goals", "Your biggest goals for the next 12 months?", "One per line.", "lines", "20 Goals/Goals.md"),
    Q("goals_school", "Goals", "Goals for school?", "Grades, subjects, the final exam. One per line.", "lines", "20 Goals/Goals.md"),
    Q("goals_work", "Goals", "Goals for your apprenticeship / work?", "One per line.", "lines", "20 Goals/Goals.md"),
    Q("goals_health", "Goals", "Goals for training and health?", "One per line.", "lines", "20 Goals/Goals.md"),
    Q("goals_money", "Goals", "Goals for money?", "Saving, spending, earning. One per line.", "lines", "20 Goals/Goals.md"),
    Q("goals_other", "Goals", "Anything else you want to achieve? Side projects, skills, travel…", "One per line.", "lines", "20 Goals/Goals.md"),
    # school
    Q("school", "School", "Which school, which year, which days?", "", "text", "40 School/School.md"),
    Q("school_subjects", "School", "Subjects, and how each is going?", "One per line, e.g. 'Math: ok, need practice'.", "lines", "40 School/School.md"),
    Q("school_tests", "School", "Upcoming tests and their dates?", "One per line: 2026-11-04 Math test (chapter 3).", "lines", "40 School/School.md"),
    # apprenticeship
    Q("apprenticeship", "Apprenticeship", "Where do you work, as what, and which year are you in?", "", "text", "50 Apprenticeship/Apprenticeship.md"),
    Q("apprenticeship_hours", "Apprenticeship", "Your usual work days and hours?", "", "text", "50 Apprenticeship/Apprenticeship.md"),
    Q("apprenticeship_lerndoku", "Apprenticeship", "How do you do the Lerndokumentation now, and what's hard about it?", "", "text", "50 Apprenticeship/Apprenticeship.md"),
    # training / health
    Q("training", "Training", "Your training: what, when, how often?", "e.g. Football Tue/Thu 19:00", "lines", "70 Health/Training.md"),
    Q("health", "Training", "Sleep, food, anything health-related Kairo should keep in mind?", "", "text", "70 Health/Training.md"),
    # people
    Q("people", "People", "The important people in your life, and how Kairo should treat their emails?",
      "One per line: 'Anna (boss): always reply within a day'.", "lines", "60 People/People.md"),
    Q("birthdays", "People", "Birthdays Kairo should remind you of?", "One per line: 03-14 Mum", "lines", "60 People/People.md"),
    # routines
    Q("routine_week", "Routines", "What does a normal week look like?", "School days, work days, training, free time."),
    Q("routine_morning", "Routines", "When do you get up and go to bed? When do you want the morning brief?"),
    Q("focus", "Routines", "When do you focus best, and what pulls you off track?"),
    # working with Kairo
    Q("annoys", "Working with Kairo", "What annoys you? (in people, apps, reminders)", "So Kairo doesn't do it."),
    Q("style", "Working with Kairo", "How should Kairo talk to you?", "Short or detailed, strict or easy-going, English or German."),
    Q("never", "Working with Kairo", "Anything Kairo should never do or never bring up?"),
]
BY_ID = {q.id: q for q in QUESTIONS}


class OnboardingError(Exception):
    pass


def answers(session: Session) -> dict[str, str]:
    return {a.question_id: a.value for a in session.exec(select(OnboardingAnswer)).all()}


def state(session: Session) -> dict:
    a = answers(session)
    answered = sum(1 for q in QUESTIONS if a.get(q.id, "").strip())
    return {"questions": [asdict(q) for q in QUESTIONS], "answers": a, "answered": answered,
            "total": len(QUESTIONS), "done": answered >= len(QUESTIONS) * 0.6}


def save(session: Session, vault_dir, question_id: str, value: str) -> dict:
    if question_id not in BY_ID:
        raise OnboardingError("unknown question")
    value = (value or "").replace("\r\n", "\n").strip()
    if len(value) > MAX_ANSWER:
        raise OnboardingError(f"answer longer than {MAX_ANSWER} characters")
    row = session.get(OnboardingAnswer, question_id)
    if value:
        row = row or OnboardingAnswer(question_id=question_id, value=value)
        row.value, row.updated_at = value, utcnow()
        session.add(row)
    elif row:
        session.delete(row)
    session.commit()
    render(session, vault_dir, files={BY_ID[question_id].file})
    return state(session)


def reset(session: Session, vault_dir) -> dict:
    for row in session.exec(select(OnboardingAnswer)).all():
        session.delete(row)
    session.commit()
    render(session, vault_dir)
    return state(session)


def render(session: Session, vault_dir, files: set[str] | None = None) -> None:
    """Write each file's 'onboarding' section from the answers (owner text outside it stays)."""
    a = answers(session)
    targets = files or {q.file for q in QUESTIONS}
    for f in targets:
        lines = [f"*From the onboarding interview. Edit it in the Kairo app (Today → Get to know you); "
                 f"Kairo rewrites only this part.*", ""]
        section = None
        for q in (q for q in QUESTIONS if q.file == f):
            value = a.get(q.id, "").strip()
            if not value:
                continue
            if q.section != section:
                lines.append(f"## {q.section}")
                section = q.section
            lines.append(f"**{q.text}**")
            if q.kind == "lines":
                lines += [f"- {line.strip()}" for line in value.split("\n") if line.strip()]
            else:
                lines.append(value)
            lines.append("")
        vault.write_section(vault_dir, f, "onboarding", "\n".join(lines))
