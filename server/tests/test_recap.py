"""Evening recap (v7 item 10): a short mail to the owner's own address, label Kairo/Recap.
No notes about other people (item 14). Through the gate (job origin, tier 2)."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session

from app import prefs, recap
from app.db import OpenLoop, PortalItem, SettingKV, Task, make_engine
from app.executor import Executor
from app.gate import load_gate
from app.profile import load_profile
from app.settings import REPO_ROOT
from tests.fakes.fake_google import FakeGoogle, connect, decode_raw

PROFILE = load_profile(REPO_ROOT / "config")
TZ = PROFILE.tz
EVENING = dt.datetime(2026, 10, 14, 20, 30, tzinfo=TZ)
UTC = dt.timezone.utc


@pytest.fixture
def env(migrated):
    fake = FakeGoogle(owner="owner@example.com")
    secrets = migrated.data_dir / "secrets"
    connect(secrets, fake)
    engine = make_engine(migrated.db_url)
    tomorrow = [{"start": "2026-10-15T07:30:00+02:00", "summary": "[Kairo] School", "all_day": False},
                {"start": "2026-10-15T18:00:00+02:00", "summary": "Football", "all_day": False}]
    r = recap.Recap(engine=engine, gate=load_gate(REPO_ROOT / "config"), tz=TZ,
                    executor=lambda: Executor(secrets_dir=secrets, transport=fake.transport(), owner_email="owner@example.com"),
                    calendar=lambda day: (tomorrow if day == dt.date(2026, 10, 15) else [], None))
    with Session(engine) as s:
        prefs.set_key(s, "recap", "20:30")                                   # off unless the owner sets a time
    yield {"engine": engine, "fake": fake, "recap": r}
    engine.dispose()


def seed(s):
    s.add(Task(title="Finish the lab report", status="done", done_at=EVENING.astimezone(UTC) - dt.timedelta(hours=3)))
    s.add(Task(title="Old thing", status="done", done_at=EVENING.astimezone(UTC) - dt.timedelta(days=3)))
    s.add(Task(title="Pay the phone bill", status="open", due=EVENING.astimezone(UTC) - dt.timedelta(days=1)))
    s.add(OpenLoop(kind="reply", source="email", what="Jo asked if Thursday 14:00 works", who="Jo", status="open",
                   since=EVENING - dt.timedelta(days=1)))
    s.add(OpenLoop(kind="people", source="people", what="Robin: haven't talked in 5 weeks; they started climbing", who="Robin",
                   status="open", since=EVENING - dt.timedelta(days=1)))
    s.add(OpenLoop(kind="promise", source="chat", what="send Kim the history notes", who="Kim", status="done",
                   closed_at=EVENING.astimezone(UTC) - dt.timedelta(hours=1), since=EVENING - dt.timedelta(days=2)))
    s.add(PortalItem(site="olat", key="file:Math/Script.pdf", kind="file", course="Math 1a", title="Script.pdf",
                     found_at=EVENING.astimezone(UTC) - dt.timedelta(hours=10)))
    s.commit()


def test_recap_mail_goes_only_to_the_owner_with_the_recap_label(env):
    with Session(env["engine"]) as s:
        seed(s)
    out = env["recap"].run(EVENING)
    assert out["sent"]
    msg = decode_raw(env["fake"].sent[-1]["raw"])
    body = msg.get_content()
    assert msg["To"] == "owner@example.com" and msg["Cc"] is None
    assert msg["Subject"] == "Kairo recap · Wednesday 14 October"
    assert "Finish the lab report" in body and "send Kim the history notes" in body          # done today
    assert "Old thing" not in body
    assert "Jo asked if Thursday 14:00 works" in body and "Pay the phone bill" in body  # still open
    assert "07:30 School" in body and "18:00 Football" in body                          # tomorrow
    assert "Math 1a: Script.pdf" in body                                            # new on OLAT
    assert "climbing" not in body and "Robin" not in body                               # item 14: no notes about people
    assert "—" not in body                                                                # no em dashes
    sent_id = env["fake"].sent[-1].get("id") or out["sent_id"]
    assert env["fake"].labels["Kairo/Recap"] in env["fake"].messages[sent_id]["labelIds"]


def test_once_a_day(env):
    assert env["recap"].run(EVENING)["sent"]
    assert not env["recap"].run(EVENING + dt.timedelta(minutes=30))["sent"]


def test_switched_off_in_settings(env):
    with Session(env["engine"]) as s:
        prefs.set_key(s, "recap", "off")
    assert env["recap"].run(EVENING) == {"sent": False, "why": "switched off"}
    assert env["fake"].sent == []


def test_off_by_default(env):
    with Session(env["engine"]) as s:                                        # a fresh install: no settings yet
        s.delete(s.get(SettingKV, prefs.KEY))
        s.commit()
    assert env["recap"].run(EVENING) == {"sent": False, "why": "switched off"}
    assert env["fake"].sent == []


def test_not_scheduled_by_default():
    from app.scheduler import cron_jobs
    assert "evening_recap" not in cron_jobs({}) and "backup" in cron_jobs({})
    schedule = {}
    prefs.apply({"recap": "19:45"}, schedule=schedule, profile=PROFILE)
    assert "evening_recap" in cron_jobs(schedule)
    prefs.apply({"recap": "off"}, schedule=schedule, profile=PROFILE)          # switched off again: unplanned
    assert "evening_recap" not in cron_jobs(schedule)


def test_recap_time_setting():
    schedule = {}
    prefs.apply({"recap": "19:45"}, schedule=schedule, profile=PROFILE)
    assert schedule["jobs"]["evening_recap"]["cron"] == "45 19 * * *"
