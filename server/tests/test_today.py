"""Today screen data (v7 redesign): streak, snapshot, top 3, next up, chips from the real day."""

from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient
from sqlmodel import Session

from app import today
from app.db import Checkin, HealthDay, Task, TriagedEmail, make_engine
from app.main import create_app
from app.profile import load_profile
from app.settings import REPO_ROOT

PROFILE = load_profile(REPO_ROOT / "config")
TZ = PROFILE.tz
NOW = dt.datetime(2026, 10, 14, 9, 0, tzinfo=TZ)   # a Wednesday


def seed(engine):
    with Session(engine) as s:
        for back in (1, 2, 3):
            s.add(Checkin(date=NOW.date() - dt.timedelta(days=back)))
        s.add(Checkin(date=NOW.date() - dt.timedelta(days=6)))
        s.add(HealthDay(date=NOW.date(), sleep_h=5.8))
        s.add(Task(title="Lab report", due=NOW - dt.timedelta(days=1), priority=1))
        s.add(Task(title="Call dentist", due=NOW + dt.timedelta(hours=3)))
        s.add(Task(title="Someday thing"))
        s.add(Task(title="Already done", status="done", done_at=NOW - dt.timedelta(hours=1)))
        s.add(TriagedEmail(message_id="m1", thread_id="t1", sender="Mr. Keller <k@school.example>", subject="Projekt",
                           category="reply", triaged_at=dt.datetime.now(dt.timezone.utc), received_at=dt.datetime.now(dt.timezone.utc)))
        s.commit()


def test_home_is_built_from_real_data(migrated):
    engine = make_engine(migrated.db_url)
    seed(engine)
    events = [{"summary": "Next week", "start": (NOW + dt.timedelta(days=7)).isoformat(), "end": None, "all_day": False, "location": None},
              {"summary": "Berufsschule", "start": (NOW + dt.timedelta(hours=2)).isoformat(), "end": None, "all_day": False, "location": "BBZ"},
              {"summary": "Old", "start": (NOW - dt.timedelta(hours=2)).isoformat(), "end": None, "all_day": False, "location": None}]
    with Session(engine) as s:
        h = today.home(s, profile=PROFILE, events=events, calendar_error=None, issues=[], now=NOW)
    assert h["streak"]["count"] == 3 and not h["streak"]["done_today"]
    assert [d["state"] for d in h["streak"]["week"]] == ["done", "done", "today", "future", "future", "future", "future"]
    assert {x["label"]: x["value"] for x in h["snapshot"]} == {"Done today": 1, "Events": 2, "Sleep": 5.8}   # "Next week" is not today
    assert [t["title"] for t in h["top3"]] == ["Lab report", "Call dentist", "Someday thing"]
    assert h["top3"][0]["why"] == "overdue"
    assert h["next_up"]["title"] == "Berufsschule" and h["next_up"]["in_minutes"] == 120
    texts = [c["text"] for c in h["suggestions"]]
    assert texts[0] == "Draft a reply to Mr. Keller" and "Prep me for Berufsschule" in texts
    assert len(texts) == 4 and h["issues"] == []


def test_endpoint_reports_calendar_problem_as_issue(migrated):
    with TestClient(create_app(migrated)) as c:
        h = c.get("/api/today").json()
    assert h["name"] and "summary" in h
    assert any(i.startswith("Calendar:") for i in h["issues"])   # no Google in tests
