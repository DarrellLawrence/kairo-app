"""Timetable (owner's request 2026-10-14): from OLAT or a photo/PDF into the calendar as FIXED school events.

One weekly block per school day, lessons in the description, repeating until the semester ends.
Through the gate (job origin, tier 2, own calendar, nobody notified)."""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session, select

from app import timetable
from app.db import CalendarTag, make_engine
from app.executor import Executor
from app.gate import load_gate
from app.profile import load_profile
from app.settings import REPO_ROOT
from tests.fakes.fake_google import FakeGoogle, connect
from tests.test_loops import Ctx

PROFILE = load_profile(REPO_ROOT / "config")
TZ = PROFILE.tz
WED = dt.date(2026, 10, 14)
READ = {"lessons": [
    {"day": "MO", "start": "07:30", "end": "08:15", "subject": "Mathematik", "room": "B204"},
    {"day": "MO", "start": "08:20", "end": "09:05", "subject": "Mathematik", "room": "B204"},
    {"day": "MO", "start": "14:00", "end": "15:30", "subject": "ABU", "room": "A12"},
    {"day": "TH", "start": "08:20", "end": "11:45", "subject": "Fachkunde", "room": None},
    {"day": "XX", "start": "08:00", "end": "09:00", "subject": "bad day"},
    {"day": "FR", "start": "10:00", "end": "09:00", "subject": "ends before it starts"},
    {"day": "FR", "start": "7am", "end": "9", "subject": "not a time"},
], "valid_until": None}


def make(migrated):
    fake = FakeGoogle()
    secrets = migrated.data_dir / "secrets"
    connect(secrets, fake)
    engine = make_engine(migrated.db_url)
    imp = timetable.Importer(engine=engine, gate=load_gate(REPO_ROOT / "config"), tz=TZ, school_class="class-x",
                             executor=lambda: Executor(secrets_dir=secrets, transport=fake.transport()))
    return engine, imp, fake


def test_semester_end():
    assert timetable.semester_end(dt.date(2026, 10, 14)) == dt.date(2027, 1, 31)
    assert timetable.semester_end(dt.date(2027, 1, 10)) == dt.date(2027, 1, 31)
    assert timetable.semester_end(dt.date(2027, 3, 1)) == dt.date(2027, 7, 15)


def test_blocks_one_per_school_day_bad_rows_dropped():
    blocks = timetable.blocks(timetable.clean(READ["lessons"]))
    assert [(b["day"], b["start"], b["end"]) for b in blocks] == [("MO", "07:30", "15:30"), ("TH", "08:20", "11:45")]
    assert "07:30–08:15 Mathematik (B204)" in blocks[0]["text"] and "14:00–15:30 ABU (A12)" in blocks[0]["text"]


def test_import_creates_fixed_weekly_school_events(migrated):
    engine, imp, fake = make(migrated)
    with Session(engine) as s:
        out = imp.import_file(Ctx(s, READ), b"%PDF timetable", "application/pdf", today=WED)
        assert out["created"] == 2
        ev = sorted(fake.events.values(), key=lambda e: "MO" not in e["recurrence"][0])   # Monday first
        assert ev[0]["summary"] == "[Kairo] School" and ev[0]["start"]["dateTime"].startswith("2026-10-19T07:30")
        assert ev[0]["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20270131T225959Z"]
        assert ev[0]["start"]["timeZone"] == "Europe/Zurich" and "attendees" not in ev[0]
        assert ev[1]["start"]["dateTime"].startswith("2026-10-15T08:20")          # Thursday: this week still
        assert all(e["_sendUpdates"] == "none" for e in ev)
        tags = {t.event_id: t.type for t in s.exec(select(CalendarTag)).all()}
        assert sorted(tags.values()) == ["FIXED", "FIXED"]
        again = imp.import_file(Ctx(s, READ), b"%PDF timetable", "application/pdf", today=WED)
        assert again["created"] == 0 and again["why"] == "already imported" and len(fake.events) == 2


def test_not_a_timetable_creates_nothing(migrated):
    engine, imp, fake = make(migrated)
    with Session(engine) as s:
        out = imp.import_file(Ctx(s, {"lessons": []}), b"%PDF", "application/pdf", today=WED)
        assert out["created"] == 0 and fake.events == {}


def test_changed_timetable_mid_semester_asks_instead_of_doubling(migrated):
    engine, imp, fake = make(migrated)
    with Session(engine) as s:
        imp.import_file(Ctx(s, READ), b"v1", "application/pdf", today=WED)
        out = imp.import_file(Ctx(s, READ), b"v2", "application/pdf", today=WED)
        assert out["created"] == 0 and out["why"] == "changed mid-semester" and len(fake.events) == 2
