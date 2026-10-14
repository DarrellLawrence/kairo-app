"""Location (privacy first): events, pause, history/delete, patterns, place reminders, tap-to-use lookup."""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import location, reviews
from app.db import LocationEvent, Task, make_engine
from app.lookup import Lookup
from app.main import create_app
from app.profile import load_profile
from app.settings import REPO_ROOT

TZ = load_profile(REPO_ROOT / "config").tz


@pytest.fixture
def client(migrated):
    app = create_app(migrated)
    sent = []
    app.state.pusher.notify = lambda title, body, url="/", **kw: sent.append((title, body))
    with TestClient(app) as c:
        c.pushes = sent
        c.app_ = app
        yield c


def auth(client):
    return {"Authorization": f"Bearer {client.get('/api/location').json()['token']}"}


def test_events_need_the_token_and_store_only_place_and_time(client, migrated):
    assert client.post("/api/location/event", json={"place": "Gym", "event": "arrive"}).status_code == 401
    h = auth(client)
    r = client.post("/api/location/event", json={"place": "gym", "event": "arrive", "lat": 47.1, "lon": 8.5}, headers=h)
    assert r.json()["stored"] is True
    assert client.post("/api/location/event", json={"place": "Gym", "event": "arrive"}, headers=h).json()["stored"] is False  # double fire
    assert client.post("/api/location/event", json={"place": "Gym", "event": "teleport"}, headers=h).status_code == 400
    assert client.post("/api/location/event", json={"place": "<script>", "event": "arrive"}, headers=h).status_code == 400
    body = client.get("/api/location").json()
    assert body["now"] == "Gym" and [e["place"] for e in body["history"]] == ["Gym"]
    engine = make_engine(migrated.db_url)
    with Session(engine) as s:
        row = s.exec(select(LocationEvent)).one()
        assert set(row.model_dump()) == {"id", "place", "event", "ts", "received_at"}   # no coordinates column at all


def test_pause_keeps_nothing_and_delete_removes(client):
    h = auth(client)
    assert client.post("/api/location/pause", json={"paused": True}).json() == {"paused": True}
    r = client.post("/api/location/event", json={"place": "Home", "event": "arrive"}, headers=h).json()
    assert r == {"ok": True, "stored": False, "paused": True}
    assert client.get("/api/location").json()["history"] == []
    client.post("/api/location/pause", json={"paused": False})
    ids = [client.post("/api/location/event", json={"place": p, "event": "arrive"}, headers=h).json()["id"] for p in ("Home", "School")]
    assert client.delete(f"/api/location/events/{ids[0]}").json() == {"deleted": 1}
    assert [e["place"] for e in client.get("/api/location").json()["history"]] == ["School"]
    assert client.delete("/api/location/events").json() == {"deleted": 1}
    assert client.get("/api/location").json()["history"] == []
    old = client.get("/api/location").json()["token"]
    new = client.post("/api/location/token/rotate").json()["token"]
    assert new != old and client.post("/api/location/event", json={"place": "Home", "event": "arrive"}, headers=h).status_code == 401


def test_arriving_reminds_of_tasks_for_that_place(client, migrated):
    engine = make_engine(migrated.db_url)
    with Session(engine) as s:
        s.add(Task(title="Bring the towel back", place="Gym"))
        s.add(Task(title="Ask about the test", place="School"))
        s.commit()
    h = auth(client)
    client.post("/api/location/event", json={"place": "Gym", "event": "arrive"}, headers=h)
    assert client.pushes == [("At Gym", "Bring the towel back")]
    client.post("/api/location/event", json={"place": "Gym", "event": "leave"}, headers=h)
    assert len(client.pushes) == 1


def seed_weeks(s, weeks=4, today=dt.date(2026, 10, 30)):
    """Mon/Wed long school days (8 h, no gym); Tue/Thu short (5 h) + gym in the evening."""
    start = today - dt.timedelta(weeks=weeks)
    d = start
    while d < today:
        if d.weekday() < 4:
            long_day = d.weekday() in (0, 2)
            a = dt.datetime.combine(d, dt.time(7, 30), tzinfo=TZ)
            b = a + dt.timedelta(hours=8 if long_day else 5)
            s.add(LocationEvent(place="School", event="arrive", ts=a))
            s.add(LocationEvent(place="School", event="leave", ts=b))
            if not long_day:
                g = dt.datetime.combine(d, dt.time(18, 0), tzinfo=TZ)
                s.add(LocationEvent(place="Gym", event="arrive", ts=g))
                s.add(LocationEvent(place="Gym", event="leave", ts=g + dt.timedelta(hours=1, minutes=30)))
        d += dt.timedelta(days=1)
    s.commit()


def test_patterns_find_skipped_training_on_long_days(migrated):
    engine = make_engine(migrated.db_url)
    with Session(engine) as s:
        seed_weeks(s)
        facts = location.patterns(s, TZ, today=dt.date(2026, 10, 30))
    text = "\n".join(facts)
    assert "School: about 4.0 times a week, usually arriving around 07:30" in text
    assert "Gym: about 2.0 times a week, usually arriving around 18:00, staying 1.5 h" in text
    assert "Gym on long days: you go less often on long school/work days (7 h or more), 0% of those days vs 100% of other weekdays." in text


def test_patterns_need_enough_data(migrated):
    engine = make_engine(migrated.db_url)
    with Session(engine) as s:
        seed_weeks(s, weeks=1)
        assert not any("long days" in f for f in location.patterns(s, TZ, today=dt.date(2026, 10, 30)))


def test_weekly_review_prefill_mentions_patterns(migrated):
    engine = make_engine(migrated.db_url)
    with Session(engine) as s:
        seed_weeks(s)
        r, _ = reviews.create(s, "monthly", dt.date(2026, 10, 25), TZ)
        assert any("Gym" in f for f in json.loads(r.prefill_json)["facts"])


def test_events_older_than_a_year_are_deleted(migrated):
    engine = make_engine(migrated.db_url)
    with Session(engine) as s:
        s.add(LocationEvent(place="Home", event="arrive", ts=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=400)))
        s.commit()
        location.record(s, place="Home", event="leave")
        assert [e.event for e in s.exec(select(LocationEvent)).all()] == ["leave"]


# ---------------------------------------------------------------- tap to use: weather + SBB

class FakeOutside:
    def __init__(self):
        self.calls = []

    def transport(self):
        def handle(req: httpx.Request):
            self.calls.append(req.url)
            if req.url.host == "api.open-meteo.com":
                return httpx.Response(200, json={"current": {"temperature_2m": 11.2, "weather_code": 61, "precipitation": 0.4},
                                                 "daily": {"temperature_2m_max": [14.0], "temperature_2m_min": [7.5],
                                                           "precipitation_probability_max": [70], "weather_code": [63]}})
            if req.url.path == "/v1/locations":
                return httpx.Response(200, json={"stations": [{"id": "8503000", "name": "Zürich HB", "distance": 240},
                                                              {"id": None, "name": "Some address"}]})
            if req.url.path == "/v1/stationboard":
                return httpx.Response(200, json={"stationboard": [{"category": "S", "number": "8", "to": "Schooltown",
                                                                   "stop": {"departure": "2026-10-14T07:42:00+0200", "platform": "33", "delay": 2}}]})
            return httpx.Response(404)
        return httpx.MockTransport(handle)


def test_lookup_rounds_coordinates_and_stores_nothing(client):
    out = FakeOutside()
    client.app_.state.lookup = Lookup(timezone="Europe/Zurich", transport=out.transport())
    r = client.post("/api/location/lookup", json={"lat": 47.378177, "lon": 8.540192}).json()
    assert r["weather"] == {"now_c": 11.2, "now": "light rain", "min_c": 7.5, "max_c": 14.0, "rain_pct": 70, "today": "rain"}
    assert r["stations"] == [{"name": "Zürich HB", "distance_m": 240, "departures": [
        {"line": "S8", "to": "Schooltown", "time": "2026-10-14T07:42:00+0200", "platform": "33", "delay": 2}]}]
    sent = " ".join(str(u) for u in out.calls)
    assert "47.378177" not in sent and "8.540192" not in sent
    assert "latitude=47.4" in sent and "x=47.38" in sent
    assert client.get("/api/location").json()["history"] == []
    activity = json.dumps(client.get("/api/activity").json())
    assert "47.37" not in activity


def test_lookup_only_calls_the_two_allowed_hosts():
    from app.lookup import LookupError_
    look = Lookup(timezone="Europe/Zurich", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with look._client() as c, pytest.raises(LookupError_):
        c.get("https://evil.example/collect?lat=1")


def test_lookup_bad_input_and_outage(client):
    assert client.post("/api/location/lookup", json={"lat": "x", "lon": 1}).status_code == 400
    assert client.post("/api/location/lookup", json={"lat": 100, "lon": 1}).status_code == 400
    client.app_.state.lookup = Lookup(timezone="Europe/Zurich", transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    r = client.post("/api/location/lookup", json={"lat": 47.3, "lon": 8.5}).json()
    assert "weather_error" in r and "transport_error" in r
