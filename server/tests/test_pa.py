"""PA engine (KICKOFF 13d): birthdays, people, money, tests, patterns, late night, leaving the house."""

from __future__ import annotations

import datetime as dt
import json

from sqlmodel import Session, select

from app import loops, pa
from app.db import CalendarTag, Flashcard, HealthDay, OpenLoop, Person, SchoolEvent, Transaction, make_engine
from app.executor import Executor
from app.gate import load_gate
from app.google.client import GoogleAPI
from app.profile import load_profile
from app.settings import REPO_ROOT
from tests.fakes.fake_google import FakeGoogle, connect
from tests.test_loops import Ctx

PROFILE = load_profile(REPO_ROOT / "config")
TZ = PROFILE.tz
WED = dt.datetime(2026, 10, 14, 10, 0, tzinfo=TZ)


def make(migrated, fake=None):
    fake = fake or FakeGoogle()
    secrets = migrated.data_dir / "secrets"
    connect(secrets, fake)
    engine = make_engine(migrated.db_url)
    vault_dir = migrated.data_dir / "vault"
    from app import vault
    vault.ensure_structure(vault_dir)
    engine_pa = pa.PA(engine=engine, profile=PROFILE, schedule={}, executor=Executor(secrets_dir=secrets, transport=fake.transport()),
                      google_read=lambda: GoogleAPI(secrets, "read", transport=fake.transport()),
                      gate=load_gate(REPO_ROOT / "config"), vault_dir=vault_dir, config_dir=REPO_ROOT / "config")
    return engine, engine_pa, fake, vault_dir


IDEAS = {"ideas": [{"idea": "Climbing chalk bag", "why": "started bouldering", "where": "Transa Bern", "price_chf": "25-35"},
                   {"idea": "Bouldering day pass", "why": "same", "where": "Magnet Bern", "price_chf": "30"},
                   {"idea": "Book: Rock Climbing", "why": "same", "where": "Orell Füssli online", "price_chf": "20-30"}]}


# ---------------------------------------------------------------- 3. birthdays

def test_birthday_ideas_five_days_before_then_eve_then_message(migrated):
    engine, p_, _, _ = make(migrated)
    with Session(engine) as s:
        s.add(Person(name="Marc", relation="friend", birthday=dt.date(2001, 10, 19), notes="2026-10-01: started bouldering",
                     email="marc@example.com"))
        s.commit()
        ctx = Ctx(s, IDEAS)
        p_.birthdays(ctx, WED)                                              # 5 days before
        loop = s.exec(select(OpenLoop).where(OpenLoop.source == "birthday")).one()
        assert loop.what.startswith("Marc's birthday is Monday 19 October (in 5 days)")
        d = loops.draft_of(loop)
        assert [i["idea"] for i in d["ideas"]][0] == "Climbing chalk bag" and "can't pay" in d["note"]
        assert "started bouldering" in ctx.prompts[0][1]
        p_.birthdays(Ctx(s), WED + dt.timedelta(days=4))                     # the eve
        assert s.exec(select(OpenLoop).where(OpenLoop.source_ref.like("%:eve"))).one().what == "Tomorrow is Marc's birthday."
        p_.birthdays(Ctx(s, {"text": "Happy birthday Marc! Have a great one.", "reason": None}), WED + dt.timedelta(days=5))
        day = s.exec(select(OpenLoop).where(OpenLoop.source_ref.like("%:day"))).one()
        assert loops.as_dict(day, WED + dt.timedelta(days=5), TZ)["can_send"] is True
        p_.birthdays(Ctx(s), WED + dt.timedelta(days=6, hours=12))           # passed: closed
        assert loops.open_loops(s) == []


def test_partner_birthday_starts_two_months_ahead(migrated):
    engine, p_, _, _ = make(migrated)
    with Session(engine) as s:
        s.add(Person(name="Lena", relation="girlfriend", birthday=dt.date(2003, 12, 1)))
        s.commit()
        p_.birthdays(Ctx(s, IDEAS, IDEAS), WED)
        loop = s.exec(select(OpenLoop).where(OpenLoop.source == "birthday")).one()
        assert "(in 48 days)" in loop.what and loop.urgency == "low"


# ---------------------------------------------------------------- 7. people

def test_people_nudge_with_reason_and_draft_by_closeness(migrated):
    engine, p_, _, _ = make(migrated)
    with Session(engine) as s:
        s.add(Person(name="Marc", relation="friend", last_contact=WED.date() - dt.timedelta(weeks=7), notes="exam last week"))
        s.add(Person(name="Mum", relation="mother", last_contact=WED.date() - dt.timedelta(days=5)))   # 14 days: not yet
        s.add(Person(name="Hans", relation="colleague", last_contact=WED.date() - dt.timedelta(days=300)))  # no rhythm
        s.commit()
        p_.people(Ctx(s, {"text": "Hey Marc, how did the exam go?", "reason": "He had his exam last week, ask how it went?"}), WED)
        loop = loops.open_loops(s)[0]
        assert loop.what == "You haven't talked to Marc in 7 weeks. He had his exam last week, ask how it went?"
        assert loops.draft_of(loop)["text"].startswith("Hey Marc") and len(loops.open_loops(s)) == 1
        from app import life
        life.people_note(s, name="Marc", contacted=True, today=WED.date())
        p_.people(Ctx(s), WED)
        assert loops.open_loops(s) == []                                     # talked: closed


# ---------------------------------------------------------------- 8. money

def _tx(s, day, amount, cat="food_delivery", merchant="Uber Eats", hour=22):
    s.add(Transaction(ts=dt.datetime.combine(day, dt.time(hour), tzinfo=TZ), amount=-amount, merchant=merchant, category=cat,
                      dedupe=f"{day}{amount}{merchant}{hour}{cat}"))


def test_unusual_spending_once_with_why_and_again_next_month(migrated):
    engine, p_, _, vault_dir = make(migrated)
    with Session(engine) as s:
        for mth in (7, 8, 9):
            _tx(s, dt.date(2026, mth, 10), 60)
        for d in (1, 3, 5, 8, 10, 12):
            _tx(s, dt.date(2026, 10, d), 30)
        s.commit()
        p_.money(Ctx(s, {"ideas": ["Cook on training days"]}), WED)
        loop = s.exec(select(OpenLoop).where(OpenLoop.source_ref == "unusual:food_delivery:2026-10")).one()
        assert loop.what == "Food delivery 180 CHF this month, usually 60 a month. Mostly uber eats, late at night." or \
            loop.what == "Food delivery 180 CHF this month, usually 60 a month. Mostly Uber Eats, late at night."
        p_.money(Ctx(s), WED)
        assert len(s.exec(select(OpenLoop).where(OpenLoop.source == "money")).all()) == 1      # once
        for d in (1, 2, 3, 4, 5, 6):
            _tx(s, dt.date(2026, 11, d), 40)
        s.commit()
        p_.money(Ctx(s, {"ideas": ["a", "b", "c"]}), dt.datetime(2026, 11, 7, 10, tzinfo=TZ))
        again = s.exec(select(OpenLoop).where(OpenLoop.source_ref == "unusual:food_delivery:2026-11")).one()
        assert again.what.startswith("Again: Food delivery 240 CHF")
        review = s.exec(select(OpenLoop).where(OpenLoop.source_ref == "review:2026-10")).first()
        assert review is None                                                 # only in the first days of a month
        p_.money(Ctx(s, {"ideas": ["Cook twice a week"]}), dt.datetime(2026, 11, 2, 10, tzinfo=TZ))
        review = s.exec(select(OpenLoop).where(OpenLoop.source_ref == "review:2026-10")).one()
        assert review.what.startswith("Money review for October 2026 is ready: 180 CHF spent")
        assert (vault_dir / "Kairo/Money/2026-10.md").read_text(encoding="utf-8").count("Cook twice a week") == 1


# ---------------------------------------------------------------- 9. tests

def test_test_in_five_days_gets_blocks_material_and_skips_are_called_out(migrated):
    engine, p_, fake, vault_dir = make(migrated)
    (vault_dir / "40 School" / "Mathe").mkdir(parents=True, exist_ok=True)
    (vault_dir / "40 School" / "Mathe" / "Ableitungen.md").write_text("Ableitung von x^2 ist 2x.", encoding="utf-8")
    with Session(engine) as s:
        test = SchoolEvent(subject="Mathe", kind="test", date=dt.datetime(2026, 10, 19, 8, tzinfo=TZ))
        s.add(test)
        s.commit()
        ctx = Ctx(s, {"title": "Ableitungen", "markdown": "# Ableitungen\n..."},
                  [{"front": "d/dx x^2", "back": "2x"}, {"front": "d/dx x^3", "back": "3x^2"}, {"front": "d/dx 5", "back": "0"}])
        p_.tests(ctx, WED)
        holds = [e for e in fake.events.values() if e["summary"] == "[Kairo] Study Mathe"]
        assert len(holds) == 4 and all(e["_sendUpdates"] == "none" for e in holds)
        assert {t.type for t in s.exec(select(CalendarTag)).all()} == {"FLEXIBLE"}
        assert len(s.exec(select(Flashcard)).all()) == 3
        loop = loops.open_loops(s)[0]
        assert loop.what == "Mathe test Monday: 4 study blocks planned, summary and 3 flashcards ready." and loop.kind == "study"
        first = min(dt.datetime.fromisoformat(e["start"]["dateTime"]) for e in holds)
        later = first + dt.timedelta(hours=2)
        p_.tests(Ctx(s), later)                                               # first block passed, no card reviewed
        skip = s.exec(select(OpenLoop).where(OpenLoop.source_ref.like("skip:%"))).one()
        assert skip.what.startswith(f"You skipped the Mathe block {first:%A %H:%M}. Moved it to")
        assert len([e for e in fake.events.values() if e["summary"] == "[Kairo] Study Mathe"]) == 5


def test_started_studying_means_no_plan(migrated):
    engine, p_, fake, _ = make(migrated)
    with Session(engine) as s:
        s.add(SchoolEvent(subject="ABU", kind="test", date=dt.datetime(2026, 10, 17, 8, tzinfo=TZ)))
        s.add(Flashcard(subject="ABU", front="a", back="b", due=WED.date(), reviewed_at=dt.datetime.now(dt.timezone.utc)))
        s.commit()
        p_.tests(Ctx(s), WED)
        assert fake.events == {} and loops.open_loops(s) == []


# ---------------------------------------------------------------- 11. patterns

def test_pattern_said_once_and_noted(migrated):
    engine, p_, _, vault_dir = make(migrated)
    with Session(engine) as s:
        start = WED.date() - dt.timedelta(days=84)
        for i in range(84):
            d = start + dt.timedelta(days=i)
            if d.weekday() in (1, 3):                                         # trains Tue + Thu
                short = i % 3 == 0
                s.add(HealthDay(date=d, sleep_h=5.2 if short else 7.5, workout_min=0 if short else 60))
        s.commit()
        out = p_.patterns(Ctx(s), WED)
        assert out["found"] == ["sleep-training"]
        loop = loops.open_loops(s)[0]
        assert loop.kind == "pattern" and loop.what.startswith("You skip training when you sleep under 6 h")
        assert "sleep under 6 h" in (vault_dir / pa.PATTERNS_NOTE).read_text(encoding="utf-8")
        loops.close(s, loop.id, "done")
        import pytest
        with pytest.raises(pa.Skipped):
            p_.patterns(Ctx(s), WED + dt.timedelta(days=1))                  # weekly
        p_.patterns(Ctx(s), WED + dt.timedelta(days=8))
        assert loops.open_loops(s) == []                                     # said once


# ---------------------------------------------------------------- 4. late night, 6. leaving

def test_late_night_nudge_once(migrated):
    engine = make_engine(migrated.db_url)
    late = dt.datetime(2026, 10, 14, 23, 40, tzinfo=TZ)
    school = dt.datetime(2026, 10, 15, 7, 0, tzinfo=TZ)
    with Session(engine) as s:
        assert pa.late_night_hint(s, now=WED, first_tomorrow=school, title="School") is None
        hint = pa.late_night_hint(s, now=late, first_tomorrow=school, title="School")
        assert "School at 07:00" in hint and "Sleep now = 6 h" in hint
        assert "already nudged" in pa.late_night_hint(s, now=late + dt.timedelta(minutes=30), first_tomorrow=school, title="School")
        assert pa.late_night_hint(s, now=dt.datetime(2026, 10, 16, 23, 45, tzinfo=TZ),
                                  first_tomorrow=dt.datetime(2026, 10, 17, 10, 0, tzinfo=TZ), title="Brunch") is None


def test_leaving_only_speaks_up_when_something_is_off():
    now = WED
    soon = (now + dt.timedelta(minutes=12)).isoformat()
    normal = [{"name": "Bern", "departures": [{"line": "S1", "to": "Thun", "time": soon, "delay": 0}]}]
    assert pa.leaving_notice(weather={"now": "Cloudy", "today": "Cloudy", "rain_pct": 20}, stations=normal, now=now) is None
    late = [{"name": "Bern", "departures": [{"line": "S1", "to": "Thun", "time": soon, "delay": 8}]}]
    assert pa.leaving_notice(weather=None, stations=late, now=now) == "S1 to Thun is 8 min late (Bern)."
    assert "Rain likely" in pa.leaving_notice(weather={"rain_pct": 80, "today": "Rain"}, stations=normal, now=now)
    assert pa.leaving_notice(weather={"today": "Snow"}, stations=[], now=now) == "Weather: Snow."
