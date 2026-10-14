"""PA rule 1: debrief by time of day, topic only when asked, routine left out, habit learned."""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session

from app import debrief
from app.db import Person, SchoolEvent, make_engine
from app.google.client import GoogleAPI
from app.profile import load_profile
from app.settings import REPO_ROOT
from tests.fakes.fake_google import FakeGoogle, connect

PROFILE = load_profile(REPO_ROOT / "config")
TZ = PROFILE.tz


def setup(migrated):
    fake = FakeGoogle()
    day = dt.date(2026, 10, 14)
    def at(d, h):
        return dt.datetime.combine(day + dt.timedelta(days=d), dt.time(h), tzinfo=TZ).isoformat()
    fake.add_event("a", summary="Dentist", start=at(0, 15))
    fake.add_event("b", summary="Training", start=at(0, 18))
    fake.events["b"]["recurringEventId"] = "weekly"                     # routine: left out
    fake.add_event("c", summary="Training", start=at(1, 19))
    fake.events["c"].update(recurringEventId="weekly", originalStartTime={"dateTime": at(1, 18)})   # moved: shown
    fake.add_event("d", summary="Project review", start=at(2, 14))
    secrets = migrated.data_dir / "secrets"
    connect(secrets, fake)
    engine = make_engine(migrated.db_url)
    with Session(engine) as s:
        s.add(SchoolEvent(subject="Mathe", kind="test", date=dt.datetime(2026, 10, 20, 8, tzinfo=TZ)))
        s.add(Person(name="Marc", relation="friend", birthday=dt.date(2000, 11, 2)))
        s.commit()
    return engine, (lambda: GoogleAPI(secrets, "read", transport=fake.transport()))


def run(engine, read, hour, **kw):
    now = dt.datetime(2026, 10, 14, hour, 0, tzinfo=TZ)
    with Session(engine) as s:
        return debrief.gather(s, now=now, tz=TZ, google_read=read, **kw)


def test_modes_follow_the_time_of_day(migrated):
    engine, read = setup(migrated)
    m = run(engine, read, 8)                     # FakeGoogle returns all; the morning window is checked by the query
    assert m["mode"] == "morning" and "dates" not in m and "today only" in m["how"]
    d = run(engine, read, 13)
    assert d["mode"] == "day" and [x["what"] for x in d["dates"]] == ["Mathe test"]
    assert d["birthdays"] == [{"name": "Marc", "date": "2026-11-02"}]
    n = run(engine, read, 21)
    assert n["mode"] == "night" and n["how"].startswith("Evening debrief: tomorrow first")


def test_routine_is_left_out_unless_it_changed(migrated):
    engine, read = setup(migrated)
    d = run(engine, read, 13)
    assert [e["summary"] for e in d["calendar"]] == ["Dentist", "Training", "Project review"]
    assert [e["changed"] for e in d["calendar"]] == [False, True, False] and d["routine_left_out"] == 1


def test_everything_and_topic(migrated):
    engine, read = setup(migrated)
    e = run(engine, read, 13, scope="everything")
    assert e["mode"] == "everything" and {"money_30d", "health_7d", "people", "holidays"} <= set(e)
    assert any(h["name"] == "Christmas" for h in e["holidays"])
    t = run(engine, read, 13, topic="school")
    assert set(t) == {"mode", "how", "now", "dates", "topic"} and t["dates"][0]["what"] == "Mathe test"


def test_learns_when_the_owner_usually_asks(migrated):
    engine, read = setup(migrated)
    for _ in range(5):
        run(engine, read, 7)
    assert run(engine, read, 8)["mode"] == "morning_full"
