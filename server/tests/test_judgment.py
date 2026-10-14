"""v8 block D: judgment never on Haiku and money on Opus; Kairo rings only for important things or
real questions, inside the call windows; the brief is a short push to read and listen to."""

from __future__ import annotations

import asyncio
import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import Session, select

from app import acceptance, models, prefs
from app.acceptance import Turn
from app.db import Call, ModelRun, OpenLoop, make_engine
from app.voice import calls
from tests.test_acceptance import ctx_for

TZ = ZoneInfo("Europe/Zurich")
WED_0700 = dt.datetime(2026, 10, 14, 7, 0, tzinfo=TZ)
WED_1100 = dt.datetime(2026, 10, 14, 11, 0, tzinfo=TZ)
SAT_0700 = dt.datetime(2026, 10, 17, 7, 0, tzinfo=TZ)
WED_0300 = dt.datetime(2026, 10, 14, 3, 0, tzinfo=TZ)


# ---------------------------------------------------------------- model routing

@pytest.mark.parametrize("purpose, override, used", [
    ("judgment", "haiku", "sonnet"),          # never Haiku, whatever the setting says
    ("proactive", "haiku", "sonnet"),
    ("coach", "haiku", "sonnet"),
    ("money_review", "sonnet", "opus"),       # money stays on Opus
    ("judgment_high", "haiku", "opus"),
    ("judgment", "opus", "opus"),             # up is fine
    ("triage", "sonnet", "sonnet"),           # simple jobs: the owner's choice
    ("reminder", None, "haiku"),
])
def test_model_routing(purpose, override, used):
    schedule = {"models": {purpose: override}} if override else {}
    assert models.pick(purpose, schedule) == used


def test_judgment_by_importance():
    assert models.judgment("high") == "judgment_high" and models.judgment("normal") == "judgment"
    assert models.pick(models.judgment("critical")) == "opus" and models.pick(models.judgment(None)) == "sonnet"


# ---------------------------------------------------------------- calls

@pytest.mark.parametrize("now, importance, question, ok", [
    (WED_0700, "high", False, True),          # important, morning commute
    (WED_0700, "normal", True, True),         # a real question, morning commute
    (WED_0700, "normal", False, False),       # small thing: never a call
    (WED_1100, "high", False, False),         # important but at work/school: push instead
    (SAT_0700, "high", False, False),         # commute windows are weekdays
    (WED_0300, "critical", False, False),     # never at night
])
def test_may_call(migrated, now, importance, question, ok):
    with Session(make_engine(migrated.db_url)) as s:
        assert calls.may_call(s, importance=importance, question=question, now=now)[0] is ok


def test_one_kairo_call_a_day_call_me_now_exempt(migrated):
    """Fix plan item 21: one shared call budget, at most one call Kairo starts a day (check-in, knowledge
    calls, anything else); the owner's own "call me now" and "Call Kairo" never count and never wait."""
    from app.db import Call
    commute = dt.datetime(2026, 10, 14, 7, 0, tzinfo=TZ)
    with Session(make_engine(migrated.db_url)) as s:
        s.add(Call(reason="Test call", opening="x", kind="owner", created_at=commute.astimezone(dt.timezone.utc)))
        s.add(Call(reason="You called Kairo", opening="", kind="owner", created_at=commute.astimezone(dt.timezone.utc)))
        s.commit()
        assert calls.may_call(s, importance="high", question=False, now=commute)[0] is True
        s.add(Call(reason="Evening check-in", opening="x", kind="kairo", created_at=commute.astimezone(dt.timezone.utc)))
        s.commit()
        ok, why = calls.may_call(s, importance="critical", question=True, now=commute + dt.timedelta(hours=12))
        assert ok is False and why == "Kairo already called once today"
        assert calls.may_call(s, importance="high", question=False, now=commute + dt.timedelta(days=1))[0] is True


def test_call_me_now_is_the_owners(migrated):
    from fastapi.testclient import TestClient

    from app.db import Call
    from app.main import create_app
    with TestClient(create_app(migrated)) as c:
        c.post("/api/voice/calls", json={})
        c.post("/api/voice/calls", json={"outgoing": True})
        with Session(make_engine(migrated.db_url)) as s:
            assert [x.kind for x in s.exec(select(Call)).all()] == ["owner", "owner"]


def test_outside_a_window_it_is_a_push_not_a_ring(migrated):
    class Pusher:
        sent = []

        def notify(self, title, body, url="/", **kw):
            self.sent.append((title, body, url))
    p = Pusher()
    with Session(make_engine(migrated.db_url)) as s:
        assert calls.proactive(s, "Bus cancelled", "Your bus is cancelled.", p, importance="high", now=WED_1100) is None
        assert p.sent == [("Kairo", "Bus cancelled", "/#today")] and s.exec(select(Call)).all() == []
        c = calls.proactive(s, "Bus cancelled", "Your bus is cancelled.", p, importance="high", now=WED_0700)
        assert c is not None and p.sent[-1][0] == "Kairo is calling"


def test_owners_call_windows_setting(migrated):
    with Session(make_engine(migrated.db_url)) as s:
        good = [{"name": "way home", "days": "weekdays", "start": "16:00", "end": "17:00"}]
        assert prefs.validate("call_windows", good) == good
        for bad in ([], [{"name": "x", "start": "22:00", "end": "02:00"}], [{"name": "x", "start": "7", "end": "8"}],
                    [{"name": "x", "days": "sometimes", "start": "07:00", "end": "08:00"}]):
            with pytest.raises(prefs.PrefsError):
                prefs.validate("call_windows", bad)


# ---------------------------------------------------------------- acceptance 19, 21, 22, 23

def test_leave_it_drops_only_the_last_thing(migrated):
    engine = make_engine(migrated.db_url)

    async def turn(text, session=None):
        if text == "leave it":
            with Session(engine) as s:
                b = s.exec(select(OpenLoop).where(OpenLoop.what.contains("library"))).one()
                b.status = "dropped"
                s.add(b)
                s.commit()
            return Turn(reply="OK, leaving the library book. Still tracking the lab report.", session="s1")
        return Turn(reply="Noted.", session="s1")
    r = asyncio.run(acceptance.t_leave_it(ctx_for(migrated, turn=turn)))
    assert r["ok"], r
    with Session(engine) as s:
        assert s.exec(select(OpenLoop)).all() == []          # both test loops cleaned up


def test_leave_it_fails_when_everything_is_dropped(migrated):
    engine = make_engine(migrated.db_url)

    async def turn(text, session=None):
        if text == "leave it":
            with Session(engine) as s:
                for loop in s.exec(select(OpenLoop)).all():
                    loop.status = "dropped"
                    s.add(loop)
                s.commit()
            return Turn(reply="Dropped everything.")
        return Turn(reply="Noted.")
    r = asyncio.run(acceptance.t_leave_it(ctx_for(migrated, turn=turn)))
    assert not r["ok"] and r["checks"]["the other one still open"] is False


@pytest.mark.parametrize("de, en, ok", [
    ("Morgen hast du Schule und am Abend Training.", "This weekend is free, nothing planned.", True),
    ("Tomorrow you have school and training.", "This weekend is free.", False),
])
def test_language_per_message(migrated, de, en, ok):
    async def turn(text, session=None):
        return Turn(reply=de if text.startswith("Was") else en, session="s1")
    assert asyncio.run(acceptance.t_language(ctx_for(migrated, turn=turn)))["ok"] is ok


def test_routing_catches_a_judgment_run_on_haiku(migrated):
    assert asyncio.run(acceptance.t_routing(ctx_for(migrated)))["ok"]
    with Session(make_engine(migrated.db_url)) as s:
        s.add(ModelRun(purpose="judgment", model="claude-haiku-4-5", ok=True))
        s.commit()
    r = asyncio.run(acceptance.t_routing(ctx_for(migrated)))
    assert not r["ok"] and "judgment:claude-haiku" in r["detail"]


def test_call_windows_catch_a_call_at_night(migrated):
    assert asyncio.run(acceptance.t_call_windows(ctx_for(migrated)))["ok"]
    night = dt.datetime.now(TZ).replace(hour=2, minute=30).astimezone(dt.timezone.utc)
    with Session(make_engine(migrated.db_url)) as s:
        s.add(Call(reason="Evening check-in", opening="Hi", status="missed", created_at=night))
        s.add(Call(reason="You called Kairo", opening="", status="ended", created_at=night))   # the owner's own call: fine
        s.commit()
    r = asyncio.run(acceptance.t_call_windows(ctx_for(migrated)))
    assert not r["ok"] and r["checks"]["every call Kairo made was in a window (14 days)"] is False
