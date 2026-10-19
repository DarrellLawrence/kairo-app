"""The owner's profile shipped with the release, imported by Kairo itself (owner's decision 2026-10-19).
Only a made-up profile here: the real one is never read, printed or quoted by a test."""

from __future__ import annotations

import datetime as dt
import json
import logging
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import Session, select

from app import diagnosis, prefs, profile_import, profile_seed
from app.actions import CalendarRule
from app.db import Capture, OpenLoop, Person, make_engine

TZ = ZoneInfo("Europe/Zurich")
TODAY = dt.date(2026, 10, 19)

FAKE = """# About Sam (seed for Kairo)
Made up for the tests.

## Basics
- Sam Example, born 01.02.2009. Home stop: Teststadt Mitte.

## School Thu
- Thursday: Maths 08:00-09:30, Physics 09:45-11:15.

## People
- Alex (sister), born 05.06.2015. Dad: birthday 01.07 (unsure).

## Goals
1. Sleep better.
"""

ANSWER = {
    "people": [{"name": "Alex", "relation": "sister", "birthday": "2015-06-05", "notes": "wants 1:1 time"},
               {"name": "Dad", "relation": "father", "birthday": "--07-01", "notes": None},
               {"name": "Broken", "relation": "x", "birthday": "01.07.", "notes": None}],          # bad date: dropped
    "loops": [{"what": "Collect the 50 from Dad", "due": None}],
    "calendar_rules": [{"match": "Training", "type": "FIXED", "days": []},
                       {"match": "*", "type": "SOCIAL", "days": ["sat", "sun"]}],
    "coach": {"training_days": ["mon", "wed"], "water_l": 2.5, "not_a_coach_key": 1},          # unknown key: dropped
    "exams": [],
    "places": [{"name": "Home", "kind": "home", "station": "Teststadt Mitte", "days": [], "via": None}],
    "goals": ["Sleep better"],
    "lessons": [{"day": "TH", "start": "08:00", "end": "09:30", "subject": "Maths", "room": None},
                {"day": "TH", "start": "09:45", "end": "11:15", "subject": "Physics", "room": None}],
    "questions": ["Is Dad's birthday really 1 July?"],
}


@pytest.fixture
def env(migrated, tmp_path):
    engine = make_engine(migrated.db_url)
    calls = {"prompts": [], "lessons": []}

    def ask(prompt):
        calls["prompts"].append(prompt)
        return json.dumps(calls.get("answer", ANSWER))

    def to_calendar(lessons):
        calls["lessons"].append(lessons)
        return {"created": 1}

    def run(text, owner="Owner"):
        with Session(engine) as s:
            return profile_seed.run(s, text=text, ask=ask, vault_dir=migrated.vault_dir, tz=TZ, today=TODAY,
                                    owner_name=owner, lessons_to_calendar=to_calendar,
                                    now=dt.datetime(2026, 10, 19, 12, tzinfo=TZ))
    return {"engine": engine, "calls": calls, "run": run, "vault": migrated.vault_dir, "settings": migrated}


def test_it_imports_once_like_save_this_to_my_profile(env, caplog):
    caplog.set_level(logging.DEBUG)
    out = env["run"](FAKE)
    assert out["status"] == "imported"
    stored = env["vault"] / "10 Profile" / "ABOUT_ME.md"
    assert stored.read_text(encoding="utf-8") == FAKE                                    # as-is
    with Session(env["engine"]) as s:
        people = {p.name: p for p in s.exec(select(Person)).all()}
        assert people["Alex"].birthday == dt.date(2015, 6, 5) and "Broken" not in people
        assert any(lp.what == "Collect the 50 from Dad" for lp in s.exec(select(OpenLoop)).all())
        coach = prefs.load(s)["coach"]
        assert coach["training_days"] == ["mon", "wed"] and coach["water_l"] == 2.5 and "not_a_coach_key" not in coach
        assert {(r["match"], tuple(r.get("days", []))) for r in profile_import.rules(s)} == {("Training", ()), ("*", ("sat", "sun"))}
        assert [p["name"] for p in prefs.load(s)["places"]] == ["Home"]
        asks = s.exec(select(Capture).where(Capture.source == "profile", Capture.status == "asking")).all()
        assert [c.question for c in asks] == ["Is Dad's birthday really 1 July?"]
    assert "Sleep better" in (env["vault"] / "20 Goals" / "Goals.md").read_text(encoding="utf-8")
    assert [x["subject"] for x in env["calls"]["lessons"][0]] == ["Maths", "Physics"]
    assert out["counts"]["people"] == 2 and out["counts"]["school_blocks"] == 1
    text = profile_seed.push_text(out)
    assert text.startswith("Your profile is set up: 2 people (2 birthdays)") and "Quick questions: 1) Is Dad" in text
    # Never logged: nothing from the profile (or the model's reading of it) is in the log.
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for secret in ("Teststadt", "Alex", "Sam Example", "Sleep better", "Maths", "1 July"):
        assert secret not in logged
    # Again: nothing to do, the model isn't asked.
    assert env["run"](FAKE)["status"] == "unchanged" and len(env["calls"]["prompts"]) == 1


def test_a_changed_file_reads_only_the_changed_sections(env):
    env["run"](FAKE)
    env["calls"]["answer"] = {"goals": ["Sleep better", "Read more"], "people": [], "loops": []}
    changed = FAKE.replace("1. Sleep better.", "1. Sleep better.\n2. Read more.")
    out = env["run"](changed)
    assert out["status"] == "imported" and out["first"] is False and out["counts"]["sections"] == 1
    assert "Only these sections changed since the last import: Goals." in env["calls"]["prompts"][-1]
    assert (env["vault"] / "10 Profile" / "ABOUT_ME.md").read_text(encoding="utf-8") == changed   # updated in place
    assert not (env["vault"] / "10 Profile" / "ABOUT_ME (2).md").exists()
    assert "Read more" in (env["vault"] / "20 Goals" / "Goals.md").read_text(encoding="utf-8")
    with Session(env["engine"]) as s:
        assert prefs.load(s)["coach"]["training_days"] == ["mon", "wed"]                   # not wiped by the partial read
    assert profile_seed.push_text(out).startswith("Your profile changes are in:")


def test_another_persons_pc_never_imports_it(env):
    assert env["run"](FAKE, owner="Jordan Other")["status"] == "not_owner"
    assert env["calls"]["prompts"] == []
    assert env["run"](FAKE, owner="Sam Example")["status"] == "imported"


def test_a_bad_answer_changes_nothing_and_is_tried_again(env):
    env["calls"]["answer"] = "not json"
    assert env["run"](FAKE)["status"] == "failed"
    with Session(env["engine"]) as s:
        assert profile_seed.state(s) == {} and s.exec(select(Person)).all() == []
    assert not (env["vault"] / "10 Profile" / "ABOUT_ME.md").exists()
    env["calls"].pop("answer")
    assert env["run"](FAKE)["status"] == "imported"


def test_weekends_are_social_but_a_training_keeps_its_own_rule():
    rules = [{"match": "Training", "type": "FIXED"}, {"match": "*", "type": "SOCIAL", "days": ["sat", "sun"]}]
    assert profile_import.rule_for(rules, "Boxing Training", "sat")["type"] == "FIXED"
    assert profile_import.rule_for(rules, "Dinner with friends", "sat")["type"] == "SOCIAL"
    assert profile_import.rule_for(rules, "Dinner with friends", "tue") is None
    with pytest.raises(ValueError):
        CalendarRule.model_validate({"match": "*", "type": "SOCIAL"})                      # "*" needs days
    assert profile_import._event_day({"start": "2026-10-24T11:15:00+02:00"}, TZ) == "sat"


def test_diagnosis_shows_the_proof_in_counts_only(env):
    env["run"](FAKE)
    st = env["settings"]
    text = diagnosis.run(db_url=st.db_url, data_dir=st.data_dir, app_root=st.data_dir / "no-app")["text"]
    part = text.split("== Owner profile")[1].split("\n==")[0]
    assert "goals: 1 note(s) in 20 Goals" in part and "coach settings: yes" in part and "2 training days" in part
    assert "people: 2 (2 with a birthday)" in part and "calendar rules: 2 (1 by weekday)" in part
    assert "quick questions still open: 1" in part
    for secret in ("Teststadt", "Alex", "Sleep better", "Maths"):
        assert secret not in part


def test_the_job_pushes_the_summary(migrated, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app
    seed = tmp_path / "private" / "profile" / "ABOUT_ME.md"
    seed.parent.mkdir(parents=True)
    seed.write_text(FAKE, encoding="utf-8")
    monkeypatch.setattr(profile_seed, "SEED", seed)
    seen = {}

    def fake_run(s, **kw):
        seen.update(kw)
        return {"status": "imported", "counts": {"people": 2, "birthdays": 1}, "questions": ["Really?"], "first": True}
    monkeypatch.setattr(profile_seed, "run", fake_run)
    app = create_app(migrated)
    pushes = []
    monkeypatch.setattr(app.state.pusher, "notify", lambda title, body, url="/", **k: pushes.append((body, k.get("source"))))
    with TestClient(app):
        run = app.state.jobs.run_job("profile_seed", forced=True)
    assert run.status == "ok" and json.loads(run.output_path)["status"] == "imported"
    assert seen["text"] == FAKE and callable(seen["lessons_to_calendar"])
    assert pushes == [("Your profile is set up: 2 people (1 birthdays). Quick questions: 1) Really?", "profile")]
