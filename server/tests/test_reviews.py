"""Reviews (v5): schedule, pre-fill, answers -> DB + vault note + tasks, API, scheduler jobs."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import reviews
from app.db import Checkin, JobRun, Review, Task, make_engine, utcnow
from app.main import create_app
from app.scheduler import Jobs, review_times
from app.profile import load_profile
from app.settings import REPO_ROOT

D = dt.date


@pytest.mark.parametrize("day,kind", [
    (D(2026, 10, 12), None),            # Monday
    (D(2026, 10, 18), "weekly"),        # ordinary Sunday
    (D(2026, 10, 25), "monthly"),       # last Sunday of October
    (D(2026, 9, 27), "quarterly"),      # last Sunday of September
    (D(2026, 6, 28), "half_year"),      # last Sunday of June
    (D(2026, 12, 27), "yearly"),        # last Sunday of December: covers month, quarter, half-year
    (D(2026, 3, 29), "quarterly"),
])
def test_biggest_review_due(day, kind):
    assert reviews.big_kind_due(day) == kind


def test_periods():
    assert reviews.period("weekly", D(2026, 10, 18)) == (D(2026, 10, 12), D(2026, 10, 18))
    assert reviews.period("quarterly", D(2026, 9, 27)) == (D(2026, 7, 1), D(2026, 9, 27))
    assert reviews.period("half_year", D(2026, 12, 27))[0] == D(2026, 7, 1)
    assert reviews.period("yearly", D(2026, 12, 27))[0] == D(2026, 1, 1)


@pytest.fixture
def db(migrated):
    return make_engine(migrated.db_url)


def test_create_is_idempotent_and_prefilled(db):
    with Session(db) as s:
        s.add(Task(title="Lab report", status="done", done_at=utcnow()))
        s.add(Task(title="Overdue thing", status="open", due=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)))
        s.commit()
        today = dt.date.today()
        r1, created1 = reviews.create(s, "weekly", today)
        r2, created2 = reviews.create(s, "weekly", today)
        assert created1 and not created2 and r1.id == r2.id
        pre = json.loads(r1.prefill_json)
        assert any("Lab report" in f for f in pre["facts"]) and any("Overdue thing" in f for f in pre["facts"])


def test_complete_writes_note_tasks_and_checkin(db, migrated):
    from app import vault
    vault.ensure_structure(migrated.vault_dir)
    with Session(db) as s:
        r, _ = reviews.create(s, "daily", D(2026, 10, 14))
        done = reviews.complete(s, migrated.vault_dir, r.id, {"energy": 4, "done": "Gym, report", "tomorrow": "Call the coach"})
        assert done.status == "done" and json.loads(done.commitments_json) == ["Call the coach"]
        note = (migrated.vault_dir / done.vault_path).read_text()
        assert "Gym, report" in note and "4/5" in note
        assert s.exec(select(Task).where(Task.source == "review")).first().title == "Call the coach"
        assert s.get(Checkin, D(2026, 10, 14)).energy_1_5 == 4
        with pytest.raises(reviews.ReviewError):
            reviews.complete(s, migrated.vault_dir, r.id, {"energy": 3})   # already closed
        r2, _ = reviews.create(s, "weekly", D(2026, 10, 18))
        for bad in ({"energy": 9}, {"nope": "x"}, {"wins": "x" * 5000}):
            with pytest.raises(reviews.ReviewError):
                reviews.complete(s, migrated.vault_dir, r2.id, bad)


def test_api_flow(migrated):
    app = create_app(migrated)
    with TestClient(app) as c:
        r = c.post("/api/reviews/start", json={"kind": "weekly"}).json()
        assert r["status"] == "open" and [q["id"] for q in r["questions"]] == ["wins", "missed", "next", "energy"]
        assert c.get("/api/reviews").json()[0]["id"] == r["id"]
        assert c.post(f"/api/reviews/{r['id']}/complete", json={"answers": {"energy": 7}}).status_code == 400
        done = c.post(f"/api/reviews/{r['id']}/complete", json={"answers": {"wins": "Shipped", "next": "A\nB\nC", "energy": 3}}).json()
        assert done["status"] == "done" and done["vault_path"].startswith("90 Reviews/")
        assert c.get("/api/reviews").json() == []
        assert c.post("/api/reviews/start", json={"kind": "hourly"}).status_code == 400
        d = c.post("/api/reviews/start", json={"kind": "daily"}).json()
        assert c.post(f"/api/reviews/{d['id']}/skip").json()["status"] == "skipped"


def test_jobs_create_reviews_push_once_and_log(db, migrated):
    sent = []
    pusher = type("P", (), {"notify": lambda self, title, body, url="/", **k: sent.append((title, url))})()
    jobs = Jobs(engine=db, pusher=pusher, profile=load_profile(REPO_ROOT / "config"))
    jobs.big_review(D(2026, 12, 27))
    jobs.big_review(D(2026, 12, 27))        # again: no second review, no second push
    jobs.daily_review(D(2026, 12, 27))
    jobs.big_review(D(2026, 12, 28))        # Monday: nothing
    assert [t for t, _ in sent] == ["Yearly review", "Evening check-in"]
    assert all(u.startswith("/#reviews/") for _, u in sent)
    with Session(db) as s:
        assert {r.kind for r in s.exec(select(Review)).all()} == {"yearly", "daily"}
        assert all(j.status == "ok" for j in s.exec(select(JobRun)).all())


def test_catch_up_after_the_pc_was_off(db):
    sent = []
    pusher = type("P", (), {"notify": lambda self, title, body, url="/", **k: sent.append(title)})()
    profile = load_profile(REPO_ROOT / "config")
    jobs = Jobs(engine=db, pusher=pusher, profile=profile)
    jobs.catch_up(dt.datetime(2026, 10, 18, 22, 0, tzinfo=profile.tz))   # Sunday night
    assert sent == ["Weekly review", "Evening check-in"]
    jobs.catch_up(dt.datetime(2026, 10, 19, 8, 0, tzinfo=profile.tz))    # Monday morning: nothing yet
    assert len(sent) == 2


def test_review_times_from_schedule():
    assert review_times({"reviews": {"daily": "20:30"}}) == {"daily": "20:30", "big": "18:00"}
    assert review_times({"reviews": {"daily": "bad"}})["daily"] == "21:00"
    assert review_times(None) == {"daily": "21:00", "big": "18:00"}


def test_a_whole_year_of_reviews_fires_on_the_right_days(db):
    """Simulate every day of 2026 at the scheduled times: 365 check-ins and the six kinds on the right Sundays."""
    import datetime as dt
    from collections import Counter
    from sqlmodel import Session, select
    from app.db import Review
    from app.profile import load_profile
    from app.scheduler import Jobs
    from app.settings import REPO_ROOT

    pushes = []

    class P:
        def notify(self, *a, **k):
            pushes.append(a)

    engine = db
    jobs = Jobs(engine=engine, pusher=P(), profile=load_profile(REPO_ROOT / "config"), schedule={})
    day = dt.date(2026, 1, 1)
    while day.year == 2026:
        jobs.big_review(day)
        jobs.daily_review(day)
        day += dt.timedelta(days=1)
    with Session(engine) as s:
        rows = s.exec(select(Review)).all()
    kinds = Counter(r.kind for r in rows)
    assert kinds["daily"] == 365
    big = {r.period_end: r.kind for r in rows if r.kind != "daily"}
    assert big[dt.date(2026, 12, 27)] == "yearly"            # last Sunday of December
    assert big[dt.date(2026, 6, 28)] == "half_year"          # last Sunday of June
    assert big[dt.date(2026, 3, 29)] == "quarterly" and big[dt.date(2026, 9, 27)] == "quarterly"
    assert big[dt.date(2026, 10, 25)] == "monthly"
    assert big[dt.date(2026, 10, 18)] == "weekly"
    assert kinds["yearly"] == 1 and kinds["half_year"] == 1 and kinds["quarterly"] == 2 and kinds["monthly"] == 8
    assert kinds["weekly"] == 52 - 12                        # every Sunday except the 12 last Sundays
    assert len(pushes) == len(rows)                          # one push each, no duplicates
