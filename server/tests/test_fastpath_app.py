"""G10 in the running app: the call line (/ws/voice, the voice model is the fake llama-server), the Today card feeding the rules,
and the timers' runner in the lifespan. Time-of-day answers are checked by shape, not by value."""

from __future__ import annotations

import datetime as dt
import re
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import prefs, voicestats
from app.db import PushLog, Timer, make_engine
from app.main import create_app
from tests.fakes.fake_google import FakeGoogle, connect
from tests.test_voice_local import local, requests_seen  # noqa: F401  (fixtures: the app with the fake voice model)
from tests.test_voicelink import frames_until, utter


def done_of(frames):
    return next(f for f in frames if isinstance(f, dict) and f["type"] == "done")


def ask(c, text, *, turn=1):
    c.voice.text = text
    with c.websocket_connect("/ws/voice") as ws:
        utter(ws, turn=turn)
        return frames_until(ws, limit=400)


def user_texts(c) -> list[str]:
    return [m["content"] for r in requests_seen(c) for m in r["messages"] if m["role"] == "user"]


def test_the_time_on_a_call_is_answered_by_a_rule_and_the_voice_model_never_hears_it(local):
    done = done_of(ask(local, "What time is it?"))
    assert re.fullmatch(r"It's (?:noon|midnight|\d{1,2}(?::\d\d)? (?:AM|PM))\.", done["text"]), done["text"]
    assert done["server"]["tool_ms"] is None and done["model"]
    assert local.voice.spoken[0][0] == done["text"]
    assert not any("What time is it" in t for t in user_texts(local))
    assert [r for r in requests_seen(local) if r.get("max_tokens") != 1] == []                    # only the call's start (priming) reached it
    with Session(make_engine(local.settings.db_url)) as s:
        got = voicestats.summary(s)
    assert (got["rule"], got["card"], got["claude"], got["talk"]) == (1, 0, 0, 0)
    msgs = [(m["role"], m["text"]) for m in local.get("/api/chat/messages").json()["messages"]]
    assert [r for r, _ in msgs] == ["owner", "kairo"] and msgs[1][1] == done["text"]            # the one conversation has it


def test_a_german_question_gets_a_german_answer(local):
    local.voice.language = "de"
    done = done_of(ask(local, "Wie spät ist es?"))
    assert re.fullmatch(r"Es ist \d{1,2} Uhr(?: \d{1,2})?\.", done["text"]), done["text"]
    assert local.voice.spoken[0][0] == done["text"]


PHONE = {"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh": "BPk", "auth": "au"}}


def test_without_a_phone_subscribed_the_call_says_it_cannot_set_a_timer_and_sets_none(local):
    done = done_of(ask(local, "Set a timer for ten minutes"))
    assert done["text"].startswith("I can't set a timer: no phone is set up for notifications yet.")
    assert local.app.state.timers.active() == [] and not any("Set a timer" in t for t in user_texts(local))   # a rule said it
    assert local.post("/api/push/subscribe", json=PHONE).status_code == 200
    assert done_of(ask(local, "Set a timer for ten minutes", turn=2))["text"] == "Okay, 10 minutes."


def test_a_timer_is_set_on_a_call_and_is_a_row_the_runner_will_ring(local):
    assert local.post("/api/push/subscribe", json=PHONE).status_code == 200
    done = done_of(ask(local, "Set a timer for ten minutes"))
    assert done["text"] == "Okay, 10 minutes."
    with Session(make_engine(local.settings.db_url)) as s:
        [row] = s.exec(select(Timer)).all()
    assert (row.seconds, row.status, row.kind, row.lang) == (600, "active", "timer", "en")
    assert [t.seconds for t in local.app.state.timers.active()] == [600]
    cancel = done_of(ask(local, "cancel the timer", turn=2))
    assert cancel["text"] == "Okay, timer cancelled." and local.app.state.timers.active() == []


def test_the_owner_can_turn_each_group_off_and_the_voice_model_answers_as_before(local):
    assert local.put("/api/flags/g10.fast_paths", json={"on": False}).status_code == 200
    assert local.put("/api/flags/g10.timers", json={"on": False}).status_code == 200
    done_of(ask(local, "What time is it?"))
    assert any("What time is it" in t for t in user_texts(local))                                 # the voice model got it
    done_of(ask(local, "Set a timer for ten minutes", turn=2))
    assert any("Set a timer for ten minutes" in t for t in user_texts(local))
    assert local.app.state.timers.active() == []                                                  # and nothing was set
    with Session(make_engine(local.settings.db_url)) as s:
        assert voicestats.summary(s)["rule"] == 0


def test_the_card_feeds_the_rules_and_only_school_blocks_keep_their_description(local, migrated):
    fake = FakeGoogle()
    connect(migrated.data_dir / "secrets", fake)
    local.app.state.google_transport = fake.transport()
    from app.profile import load_profile
    from app.settings import REPO_ROOT
    tz = load_profile(REPO_ROOT / "config").tz
    local_day = (dt.datetime.now(tz) + dt.timedelta(days=1)).date().isoformat()
    off = (dt.datetime.now(tz) + dt.timedelta(days=1)).replace(hour=12).strftime("%z")      # tomorrow's offset (a clock change in between)
    off = off[:3] + ":" + off[3:]
    fake.add_event("e1", summary="Dentist", start=f"{local_day}T14:00:00{off}", end=f"{local_day}T15:00:00{off}")
    fake.add_event("e2", summary="School", start=f"{local_day}T09:00:00{off}", end=f"{local_day}T12:00:00{off}")
    fake.events["e2"]["description"] = "Class 3a\n09:00–09:45 Biology (R3)\n10:00–10:45 Maths (R12)"
    fake.events["e1"]["description"] = "private notes that must not be kept"
    local.app.state.today_card.sources["events"]()
    facts = local.app.state.fast.facts
    events = facts.events(dt.datetime.now(tz).date())
    assert events is not None and {e["summary"] for e in events} == {"Dentist", "School"}
    assert "description" not in next(e for e in events if e["summary"] == "Dentist")
    assert "Biology" in next(e for e in events if e["summary"] == "School")["description"]
    assert done_of(ask(local, "What is my next lesson?"))["text"] == "Your next lesson is Biology tomorrow at 9 AM, in room R3."
    assert done_of(ask(local, "When is my next appointment?", turn=2))["text"] == "Your next appointment is School tomorrow at 9 AM."
    fake.calendar_down = True
    local.app.state.today_card.sources["events"]()                       # the read fails: the card keeps its old words, the rules know nothing
    assert facts.events(dt.datetime.now(tz).date()) is None
    before = len(requests_seen(local))
    done_of(ask(local, "When is my next appointment?", turn=3))
    assert len(requests_seen(local)) > before                              # the voice model answers; no rule guessed


def test_the_card_feeds_the_next_connection(local):
    from app.profile import load_profile
    from app.settings import REPO_ROOT
    tz = load_profile(REPO_ROOT / "config").tz
    with Session(make_engine(local.settings.db_url)) as s:
        prefs.place_set(s, {"name": "Home", "kind": "home", "station": "Olten"})
        prefs.place_set(s, {"name": "School", "kind": "school", "station": "Bern"})
    soon = (dt.datetime.now(tz) + dt.timedelta(minutes=30)).isoformat()
    local.app.state.lookup.connections = lambda frm, to, via=None, limit=3: [
        {"departure": soon, "arrival": soon, "delay": 0, "line": "IC8", "platform": "4", "cancelled": False}]
    local.app.state.today_card.on_call = True                              # the timetable rests at night, not on a call
    local.app.state.today_card.sources["trip"]()
    got = done_of(ask(local, "When is the next train?"))["text"]
    assert re.fullmatch(r"The next train to School leaves Home at \d{1,2}:\d\d (?:AM|PM), platform 4\.", got), got
    assert not any("next train" in t for t in user_texts(local))


def test_the_timers_runner_starts_with_the_app_and_rings_through_the_real_pusher(migrated, monkeypatch):
    monkeypatch.setenv("KAIRO_SCHEDULER", "1")                              # the lifespan starts its background work only with this
    monkeypatch.setattr("app.main.acceptance_after_release", lambda app: _never())
    app = create_app(migrated)
    sent = []
    app.state.pusher._send = lambda **kw: sent.append(kw)
    monkeypatch.setattr("app.push.in_quiet_hours", lambda *a, **k: True)       # the middle of the night
    app.state.jobs.start = lambda: None
    with TestClient(app) as c:
        c.post("/api/push/subscribe", json={"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh": "BPk", "auth": "au"}})
        app.state.timers.add(1, label="tea")
        deadline = time.monotonic() + 20
        while not sent and time.monotonic() < deadline:
            time.sleep(0.1)
    assert sent and sent[0]["ttl"] == 600 and sent[0]["headers"] == {"Urgency": "high"} and "tea" in sent[0]["data"]
    with Session(make_engine(migrated.db_url)) as s:
        [row] = s.exec(select(Timer)).all()
        assert (row.status, [p.status for p in s.exec(select(PushLog)).all()]) == ("fired", ["sent"])


def test_a_push_service_that_is_down_leaves_the_timer_waiting_and_the_log_says_failed(migrated, monkeypatch):
    """Through the lifespan, the real Timers and the real Pusher: the sender fails, the timer is not marked rung, the next try
    (the sender works again) rings it with the same tag."""
    monkeypatch.setenv("KAIRO_SCHEDULER", "1")
    monkeypatch.setattr("app.main.acceptance_after_release", lambda app: _never())
    monkeypatch.setattr("app.timers.RETRY_S", 0.2)
    app = create_app(migrated)
    sent, fail = [], [True]

    def sender(**kw):
        if fail[0]:
            raise TimeoutError("push service timed out")
        sent.append(kw)

    app.state.pusher._send = sender
    app.state.jobs.start = lambda: None
    with TestClient(app) as c:
        c.post("/api/push/subscribe", json=PHONE)
        app.state.timers.add(1, label="tea")
        deadline = time.monotonic() + 20
        with Session(make_engine(migrated.db_url)) as s:
            while time.monotonic() < deadline and not s.exec(select(PushLog).where(PushLog.status == "failed")).all():
                s.expire_all()
                time.sleep(0.1)
        fail[0] = False
        while not sent and time.monotonic() < deadline:
            time.sleep(0.1)
    with Session(make_engine(migrated.db_url)) as s:
        [row] = s.exec(select(Timer)).all()
        statuses = [p.status for p in s.exec(select(PushLog).order_by(PushLog.id)).all()]
    assert sent and "tea" in sent[0]["data"] and row.status == "fired"
    assert statuses[0] == "failed" and statuses[-1] == "sent"


async def _never():
    import asyncio
    await asyncio.sleep(3600)
