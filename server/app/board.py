"""Board (KICKOFF 11, Milestone D): goals > projects > tasks, as Kanban and timeline.

Owner-side API only (the app). The model keeps using tasks.* through the gate.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from .db import Goal, Project, SchoolEvent, Task, utcnow

PLACE = r"^[\w][\w .'&-]{0,39}$"
TaskStatus = Literal["open", "doing", "done", "dropped"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GoalIn(_In):
    title: str = Field(min_length=1, max_length=200)
    area: str | None = Field(default=None, max_length=60)
    horizon: Literal["week", "month", "quarter", "year"] = "quarter"
    target_date: dt.date | None = None
    status: Literal["active", "done", "dropped"] = "active"


class GoalPatch(_In):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    area: str | None = Field(default=None, max_length=60)
    horizon: Literal["week", "month", "quarter", "year"] | None = None
    target_date: dt.date | None = None
    status: Literal["active", "done", "dropped"] | None = None


class ProjectIn(_In):
    title: str = Field(min_length=1, max_length=200)
    area: str | None = Field(default=None, max_length=60)
    goal_id: int | None = None
    status: Literal["active", "done", "dropped"] = "active"


class ProjectPatch(_In):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    area: str | None = Field(default=None, max_length=60)
    goal_id: int | None = None
    status: Literal["active", "done", "dropped"] | None = None


class TaskIn(_In):
    title: str = Field(min_length=1, max_length=300)
    project_id: int | None = None
    due: dt.datetime | None = None
    priority: int = Field(default=0, ge=0, le=3)
    status: TaskStatus = "open"
    place: str | None = Field(default=None, pattern=PLACE)


class TaskPatch(_In):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    project_id: int | None = None
    due: dt.datetime | None = None
    priority: int | None = Field(default=None, ge=0, le=3)
    status: TaskStatus | None = None
    place: str | None = Field(default=None, pattern=PLACE)
    clear: list[Literal["due", "project_id", "place"]] = []   # set these to empty


class BoardError(ValueError):
    pass


def _check_refs(s: Session, *, goal_id=None, project_id=None) -> None:
    if goal_id is not None and s.get(Goal, goal_id) is None:
        raise BoardError(f"no goal {goal_id}")
    if project_id is not None and s.get(Project, project_id) is None:
        raise BoardError(f"no project {project_id}")


def goal_dict(g: Goal) -> dict:
    return {"id": g.id, "title": g.title, "area": g.area, "horizon": g.horizon,
            "target_date": g.target_date.isoformat() if g.target_date else None, "status": g.status}


def project_dict(p: Project) -> dict:
    return {"id": p.id, "title": p.title, "area": p.area, "goal_id": p.goal_id, "status": p.status}


def task_dict(t: Task) -> dict:
    due = t.due if (t.due is None or t.due.tzinfo) else t.due.replace(tzinfo=dt.timezone.utc)
    return {"id": t.id, "title": t.title, "project_id": t.project_id, "due": due.isoformat() if due else None,
            "priority": t.priority, "status": t.status, "source": t.source, "place": t.place,
            "done_at": t.done_at.isoformat() if t.done_at else None}


def board(s: Session, *, done_days: int = 14) -> dict:
    since = utcnow() - dt.timedelta(days=done_days)
    goals = s.exec(select(Goal).where(Goal.status == "active").order_by(Goal.target_date.is_(None), Goal.target_date)).all()
    projects = s.exec(select(Project).where(Project.status == "active").order_by(Project.title)).all()
    tasks = s.exec(select(Task).where(Task.status.in_(["open", "doing"]))).all()
    done = s.exec(select(Task).where(Task.status == "done").where(Task.done_at >= since)).all()
    school = s.exec(select(SchoolEvent).where(SchoolEvent.date >= since).order_by(SchoolEvent.date)).all()
    return {"goals": [goal_dict(g) for g in goals], "projects": [project_dict(p) for p in projects],
            "tasks": [task_dict(t) for t in sorted(tasks, key=lambda t: (-t.priority, t.id))] +
                     [task_dict(t) for t in sorted(done, key=lambda t: t.done_at, reverse=True)],
            "school": [{"id": e.id, "subject": e.subject, "kind": e.kind, "date": e.date.isoformat()} for e in school]}


def create(s: Session, model, data) -> object:
    if isinstance(data, ProjectIn):
        _check_refs(s, goal_id=data.goal_id)
    if isinstance(data, TaskIn):
        _check_refs(s, project_id=data.project_id)
        row = Task(**data.model_dump(), source="board")
        if row.place:
            row.place = row.place[:1].upper() + row.place[1:]
        if row.status == "done":
            row.done_at = utcnow()
    else:
        row = model(**data.model_dump())
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


def update(s: Session, model, row_id: int, patch) -> object:
    row = s.get(model, row_id)
    if row is None:
        raise BoardError(f"no {model.__tablename__[:-1]} {row_id}")
    fields = patch.model_dump(exclude_unset=True, exclude={"clear"})
    if "goal_id" in fields:
        _check_refs(s, goal_id=fields["goal_id"])
    if "project_id" in fields:
        _check_refs(s, project_id=fields["project_id"])
    for k, v in fields.items():
        if v is not None:
            setattr(row, k, v)
    for k in getattr(patch, "clear", []):
        setattr(row, k, None)
    if isinstance(row, Task):
        if fields.get("status") == "done" and row.done_at is None:
            row.done_at = utcnow()
        elif fields.get("status") in ("open", "doing"):
            row.done_at = None
        if row.place:
            row.place = row.place[:1].upper() + row.place[1:]
    s.add(row)
    s.commit()
    s.refresh(row)
    return row
