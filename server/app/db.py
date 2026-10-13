"""SQLModel models (KICKOFF section 9) and the database engine.

Schema changes go through Alembic (server/migrations). Never call
SQLModel.metadata.create_all() in app code.
"""

from __future__ import annotations

import datetime as dt
from datetime import datetime, timezone

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, Session, SQLModel, create_engine


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Goal(SQLModel, table=True):
    __tablename__ = "goals"
    id: int | None = Field(default=None, primary_key=True)
    title: str
    area: str | None = None
    horizon: str = Field(index=True)  # week / month / quarter / year
    target_date: dt.date | None = None
    status: str = Field(default="active", index=True)
    vault_path: str | None = None


class Project(SQLModel, table=True):
    __tablename__ = "projects"
    id: int | None = Field(default=None, primary_key=True)
    title: str
    area: str | None = None
    goal_id: int | None = Field(default=None, foreign_key="goals.id", index=True)
    status: str = Field(default="active", index=True)
    vault_path: str | None = None


class Task(SQLModel, table=True):
    __tablename__ = "tasks"
    id: int | None = Field(default=None, primary_key=True)
    title: str
    project_id: int | None = Field(default=None, foreign_key="projects.id", index=True)
    due: datetime | None = Field(default=None, index=True)
    priority: int = 0
    status: str = Field(default="open", index=True)
    source: str = "chat"  # capture / review / chat / email
    created_at: datetime = Field(default_factory=utcnow)
    done_at: datetime | None = None


class Capture(SQLModel, table=True):
    __tablename__ = "captures"
    id: int | None = Field(default=None, primary_key=True)
    kind: str  # text / audio / photo / pdf / link
    raw_path: str | None = None
    text: str | None = Field(default=None, sa_column=Column(Text))
    processed: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=utcnow)


class Approval(SQLModel, table=True):
    __tablename__ = "approvals"
    id: int | None = Field(default=None, primary_key=True)
    tool: str
    args_json: str = Field(sa_column=Column(Text, nullable=False))
    summary: str
    tier: int
    status: str = Field(default="open", index=True)  # open/approved/rejected/expired/done
    created_at: datetime = Field(default_factory=utcnow)
    decided_at: datetime | None = None
    result_json: str | None = Field(default=None, sa_column=Column(Text))
    thread_id: int | None = Field(default=None, foreign_key="chat_threads.id")
    reason: str | None = Field(default=None, sa_column=Column(Text))  # the model's stated reason (shown, never trusted)


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_log"
    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    actor: str  # brain / backend / owner
    action: str
    tier: int | None = None
    args_hash: str | None = None
    outcome: str


class JobRun(SQLModel, table=True):
    __tablename__ = "job_runs"
    id: int | None = Field(default=None, primary_key=True)
    job: str = Field(index=True)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    status: str = "running"
    output_path: str | None = None
    error: str | None = Field(default=None, sa_column=Column(Text))


class Checkin(SQLModel, table=True):
    __tablename__ = "checkins"
    date: dt.date = Field(primary_key=True)
    energy_1_5: int | None = None
    done_text: str | None = Field(default=None, sa_column=Column(Text))
    top3_json: str | None = None
    note: str | None = Field(default=None, sa_column=Column(Text))


class Review(SQLModel, table=True):
    __tablename__ = "reviews"
    __table_args__ = (UniqueConstraint("kind", "period_start", name="uq_reviews_kind_period"),)
    id: int | None = Field(default=None, primary_key=True)
    kind: str = Field(index=True)  # daily/weekly/monthly/quarterly/half_year/yearly
    period_start: dt.date
    period_end: dt.date | None = None
    vault_path: str | None = None
    commitments_json: str | None = None
    status: str = Field(default="open", index=True)  # open / done / skipped
    prefill_json: str | None = Field(default=None, sa_column=Column(Text))   # what Kairo gathered
    answers_json: str | None = Field(default=None, sa_column=Column(Text))   # the owner's answers
    created_at: datetime | None = Field(default_factory=utcnow)
    completed_at: datetime | None = None


class OnboardingAnswer(SQLModel, table=True):
    """The owner's onboarding answers (source of truth; the vault files are rendered from them)."""

    __tablename__ = "onboarding_answers"
    question_id: str = Field(primary_key=True)
    value: str = Field(sa_column=Column(Text, nullable=False))
    updated_at: datetime = Field(default_factory=utcnow)


class SchoolEvent(SQLModel, table=True):
    __tablename__ = "school_events"
    id: int | None = Field(default=None, primary_key=True)
    subject: str
    kind: str  # test / deadline / lesson
    date: datetime = Field(index=True)
    notes: str | None = Field(default=None, sa_column=Column(Text))


class Grade(SQLModel, table=True):
    __tablename__ = "grades"
    id: int | None = Field(default=None, primary_key=True)
    subject: str = Field(index=True)
    date: dt.date
    grade: float
    weight: float = 1.0


class Lerndoku(SQLModel, table=True):
    __tablename__ = "lerndoku"
    week: str = Field(primary_key=True)  # ISO week, e.g. 2026-W42
    raw_notes: str | None = Field(default=None, sa_column=Column(Text))
    draft_path: str | None = None
    status: str = "draft"  # draft / approved


class ChatThread(SQLModel, table=True):
    __tablename__ = "chat_threads"
    id: int | None = Field(default=None, primary_key=True)
    brain: str
    brain_session_id: str | None = None
    started_at: datetime = Field(default_factory=utcnow, index=True)
    title: str | None = None


class ChatMessage(SQLModel, table=True):
    """Not in KICKOFF section 9: the Chat screen needs history to display.

    The CLI keeps its own transcript for context; this table is only for the UI.
    """

    __tablename__ = "chat_messages"
    id: int | None = Field(default=None, primary_key=True)
    thread_id: int = Field(foreign_key="chat_threads.id", index=True)
    role: str  # owner / kairo / system
    text: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=utcnow)


class PushSubscription(SQLModel, table=True):
    __tablename__ = "push_subscriptions"
    id: int | None = Field(default=None, primary_key=True)
    endpoint: str = Field(unique=True)
    keys_json: str
    device: str | None = None


class SettingKV(SQLModel, table=True):
    __tablename__ = "settings"
    key: str = Field(primary_key=True)
    value: str | None = Field(default=None, sa_column=Column(Text))


def make_engine(db_url: str):
    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    return create_engine(db_url, connect_args=connect_args)


def session_for(engine) -> Session:
    return Session(engine, expire_on_commit=False)
