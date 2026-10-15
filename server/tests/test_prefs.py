"""Settings by voice / More -> Settings (instead of editing config), places, leaving the house."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app import pa, prefs
from app.main import create_app
from app.profile import load_profile
from app.settings import REPO_ROOT

PROFILE = load_profile(REPO_ROOT / "config")
TZ = PROFILE.tz


@pytest.fixture
def client(migrated):
    app = create_app(migrated)
    with TestClient(app) as c:
        c.app_ = app
        yield c


def test_settings_apply_at_once(client):
    jobs = client.app_.state.jobs
    r = client.put("/api/settings/quiet_hours", json={"value": {"start": "23:30", "end": "06:00"}})
    assert r.status_code == 200 and r.json()["settings"]["quiet_hours"] == {"start": "23:30", "end": "06:00"}
    assert jobs.quiet == ("23:30", "06:00") and PROFILE is not None
    client.put("/api/settings/brief_times", json={"value": {"weekdays": "06:45", "weekends": "09:00"}})
    assert jobs.schedule["jobs"]["morning_brief"] == {"weekdays": "45 6 * * 1-5", "weekends": "0 9 * * 6,0"}
    client.put("/api/settings/max_pings", json={"value": 9})
    assert jobs.schedule["proactive"]["max_per_day"] == 9
    assert client.put("/api/settings/max_pings", json={"value": 99}).status_code == 400
    assert client.put("/api/settings/gate", json={"value": "off"}).status_code == 400
    assert client.put("/api/settings/brief_times", json={"value": {"weekdays": "6am"}}).status_code == 400


def test_places_by_voice_tool_and_app(client):
    from app.gate import load_gate
    from app.tools import ToolRunner
    from app.db import make_engine
    from tests.test_gate import ctx as gctx
    gate = load_gate(REPO_ROOT / "config")
    runner = ToolRunner(engine=make_engine(client.app_.state.settings.db_url), vault_dir=client.app_.state.settings.vault_dir,
                        google_read=lambda: None, profile=PROFILE)
    changed = []
    runner.on_settings_change = lambda: changed.append(1)
    run = lambda tool, args: runner.run(gate.decide("mcp__kairo__" + tool, args, gctx()))  # noqa: E731
    assert run("place_set", {"name": "Home", "kind": "home", "station": "Testdorf", "address": "Musterweg 1"}) == \
        "Saved Home (home, station Testdorf)."
    run("place_set", {"name": "Old job", "kind": "work", "station": "A", "valid_until": "2027-07-31"})
    run("place_set", {"name": "New job", "kind": "work", "station": "B", "valid_from": "2027-08-01"})
    assert changed == [1, 1, 1]
    names = [p["name"] for p in client.get("/api/settings").json()["places"]]
    assert names[-3:] == ["Home", "Old job", "New job"]
    from app.db import make_engine as me
    from sqlmodel import Session
    with Session(me(client.app_.state.settings.db_url)) as s:
        for name in [p.name for p in prefs.places(s)]:
            if name not in ("Home", "Old job", "New job"):
                prefs.place_remove(s, name)                                 # the seeds from profile.yaml
        assert prefs.active_place(s, "work", dt.date(2027, 7, 1)).name == "Old job"
        assert prefs.active_place(s, "work", dt.date(2027, 9, 1)).name == "New job"
    assert run("place_remove", {"name": "old job"}) == "Removed."
    assert "Saved quiet_hours" in run("settings_set", {"key": "quiet_hours", "value": {"start": "23:00", "end": "06:30"}})
    assert run("settings_set", {"key": "language", "value": "fr"}).startswith("Error")


def test_destination_from_calendar_or_today_place():
    now = dt.datetime(2026, 10, 14, 7, 0, tzinfo=TZ)
    places = [prefs.Place(name="Home", kind="home", station="H"),
              prefs.Place(name="School", kind="school", station="S", address="Musterhaus Schulstrasse 5"),
              prefs.Place(name="Training", kind="training", station="T")]
    ev = [{"summary": "ABU", "location": "Schulstrasse 5, Testingen", "start": "2026-10-14T08:00:00+02:00"}]
    assert pa.destination(places, ev, now)[0].name == "School"
    ev = [{"summary": "Football at Training", "location": None, "start": "2026-10-14T18:00:00+02:00"}]
    assert pa.destination(places, ev, now)[0].name == "Training"
    assert pa.destination(places, [], now)[0].name == "School"


def test_leaving_speaks_up_for_delay_cancellation_or_a_better_connection():
    now = dt.datetime(2026, 10, 14, 7, 0, tzinfo=TZ)
    def c(dep, arr, delay=0, cancelled=False):
        return {"departure": f"2026-10-14T{dep}:00+0200", "arrival": f"2026-10-14T{arr}:00+0200", "delay": delay,
                "line": "S5", "cancelled": cancelled}
    assert pa.leaving_notice(weather=None, stations=None, now=now, connections=[c("07:10", "07:40")], to="School") is None
    msg = pa.leaving_notice(weather=None, stations=None, now=now, connections=[c("07:10", "07:40", delay=12), c("07:20", "07:47")],
                            to="School")
    assert msg == "S5 07:10 to School is 12 min late. Take the 07:20 instead, it gets there first."
    assert pa.leaving_notice(weather=None, stations=None, now=now, connections=[c("07:10", "07:40", cancelled=True)],
                             to="School").startswith("Your 07:10 S5 to School is cancelled.")
