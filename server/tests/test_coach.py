"""v8 E: coaching modules. Settings, the plan rules (the code enforces them, not the model), streaks,
every nudge rule (training, leg day, food, water, study, snacks), the job, study plans, the tools, money
questions on Opus, and the acceptance tests 24-28 passing when Kairo behaves and failing when it doesn't.
On kairo-home the acceptance tests run against the real model with the owner's real settings."""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import pytest
from sqlmodel import Session, select

from app import acceptance, coach, life, prefs
from app.acceptance import Turn
from app.chat import AskResult
from app.db import CoachLog, OpenLoop, Person, SchoolEvent, SettingKV, WishItem, make_engine
from tests.test_acceptance import ctx_for
from tests.test_internal_tools import setup  # noqa: F401  (fixture)
from tests.test_loops import Ctx, Pushes
from tests.test_pa import PROFILE, TZ, make

MON = dt.date(2026, 10, 19)                      # a Monday
CFG = {"training_days": ["mon", "thu"], "home_sessions": 2, "home_minutes": 25,
       "equipment": ["Kettlebell 16 kg", "Resistance band"], "water_l": 2.0, "food_check": "20:00",
       "work_hours": "08:00-17:00", "faith_habit": "a short prayer", "faith_after": "coffee", "snacks_week_chf": 20}


def at(day: dt.date, hh: int, mm: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hh, mm), tzinfo=TZ)


@pytest.fixture
def s(migrated):
    with Session(make_engine(migrated.db_url)) as session:
        prefs.set_key(session, "coach", CFG)
        yield session


def model_plan(**over):
    raw = {"sessions": [
        {"day": "2026-10-19", "focus": "upper", "minutes": 20, "exercises": [{"name": "Push-ups", "dose": "3x10", "equipment": "bodyweight"}]},
        {"day": "2026-10-20", "focus": "legs", "minutes": 45, "exercises": [
            {"name": "Goblet squat", "dose": "4x10", "equipment": "kettlebell"},
            {"name": "Leg press", "dose": "3x12", "equipment": "leg press machine"}]},
        {"day": "2026-10-22", "focus": "core", "minutes": 20, "exercises": [{"name": "Plank", "dose": "3x40s", "equipment": "none"}]},
        {"day": "2026-10-24", "focus": "mobility", "minutes": 15, "exercises": [{"name": "Band pull-apart", "dose": "3x15",
                                                                                   "equipment": "resistance band"}]},
    ], "food": ["Protein at every meal."]}
    raw.update(over)
    return raw


# ---------------------------------------------------------------- settings

def test_coach_settings_change_one_fact_at_a_time(s):
    prefs.set_key(s, "coach", {"water_l": 2.5})
    c = prefs.coach(s)
    assert c.water_l == 2.5 and c.training_days == ["mon", "thu"] and c.home_minutes == 25
    prefs.set_key(s, "coach", {"food_check": None})                      # off
    assert prefs.coach(s).food_check is None
    for bad in ({"training_days": ["monday"]}, {"home_minutes": 200}, {"water_l": 20}, {"work_hours": "17:00-08:00"},
                {"savings": [{"name": "x", "chf": -1}]}, {"pay_rent": True}):
        with pytest.raises(prefs.PrefsError):
            prefs.set_key(s, "coach", bad)
    assert "coach" in prefs.public_view(s)["settings"]


# ---------------------------------------------------------------- the plan: the code keeps the rules

def test_the_plan_keeps_the_owners_rules():
    cfg = prefs.Coach.model_validate(CFG)
    p = coach.enforce(model_plan(), cfg, MON)
    days = [x["day"] for x in p["sessions"]]
    assert "2026-10-19" not in days and "2026-10-22" not in days            # Monday and Thursday are training days
    assert days == ["2026-10-20", "2026-10-24"]                             # the set number, in order
    legs = p["sessions"][0]
    assert legs["minutes"] == 25                                            # 45 asked, 25 allowed
    assert [e["name"] for e in legs["exercises"]] == ["Goblet squat"]       # no leg press machine at home
    assert p["leg_day"] == "2026-10-20" and p["fixed"] == ["2026-10-19", "2026-10-22"]
    assert coach.enforce({"sessions": "nonsense"}, cfg, MON)["sessions"] == []


def test_no_equipment_means_bodyweight_only():
    cfg = prefs.Coach.model_validate({**CFG, "equipment": []})
    p = coach.enforce(model_plan(), cfg, MON)
    assert all(e["equipment"].lower() in coach.BODYWEIGHT for x in p["sessions"] for e in x["exercises"])


def test_the_job_plans_the_week_writes_the_note_and_nudges(s, migrated):
    vault_dir = migrated.data_dir / "vault"
    c = coach.Coacher(engine=s.get_bind(), profile=PROFILE, schedule={}, vault_dir=vault_dir)
    ctx = Ctx(s, model_plan())
    ctx.jobs = type("J", (), {"pusher": Pushes()})()
    r = json.loads(c.run(ctx, at(MON - dt.timedelta(days=1), 17, 30)))    # Sunday evening: next week
    assert r["plan"] == "Kairo/Coach/Training 2026-10-19.md" and ctx.prompts[0][0] == "coach"
    note = (vault_dir / r["plan"]).read_text(encoding="utf-8")
    assert "Fixed training: Mon, Thu" in note and "Goblet squat" in note and "Leg press" not in note
    assert c.run(ctx, at(MON - dt.timedelta(days=1), 18)) and len(ctx.prompts) == 1      # planned once
    # Tuesday 17:30: today's session; Wednesday morning, nothing logged: leg day pushback
    c.run(ctx, at(MON + dt.timedelta(days=1), 17, 30))
    loop = s.exec(select(OpenLoop).where(OpenLoop.source_ref == "training:2026-10-20")).one()
    assert loop.what == "Today: 25 min legs at home. Done? Tell me." and loop.kind == "coach"
    c.run(ctx, at(MON + dt.timedelta(days=2), 7))
    miss = s.exec(select(OpenLoop).where(OpenLoop.source_ref == "training:missed:2026-10-20")).one()
    assert miss.what.startswith("Leg day skipped yesterday") and miss.status == "open"
    assert s.get(OpenLoop, loop.id).status == "stale"                       # yesterday's reminder is gone
    coach.log_entry(s, module="training", what="legs, done late", day=MON + dt.timedelta(days=2))
    assert s.get(OpenLoop, miss.id).status == "done"                         # doing it today closes the pushback


def test_a_failing_plan_is_not_retried_every_half_hour(s, migrated):
    c = coach.Coacher(engine=s.get_bind(), profile=PROFILE, schedule={}, vault_dir=migrated.data_dir / "vault")

    class Down(Ctx):
        def ask(self, prompt, system, images=None, purpose=None):
            self.prompts.append((purpose, prompt))
            return AskResult("", status="error", error="model unavailable")
    ctx = Down(s)
    ctx.jobs = type("J", (), {"pusher": Pushes()})()
    r = json.loads(c.run(ctx, at(MON, 20)))
    assert r["plan"].startswith("failed") and r["nudges"] == 1                 # the food check still went out
    c.run(ctx, at(MON, 20, 30))
    c.run(ctx, at(MON, 22))
    assert len(ctx.prompts) == 1                                             # next try after 3 hours
    c.run(ctx, at(MON, 23, 30))
    assert len(ctx.prompts) == 2


def test_coaching_is_off_until_the_owner_sets_it(migrated):
    from app.scheduler import Skipped
    with Session(make_engine(migrated.db_url)) as s:
        c = coach.Coacher(engine=s.get_bind(), profile=PROFILE, schedule={}, vault_dir=migrated.data_dir / "vault")
        with pytest.raises(Skipped, match="no coach settings"):
            c.run(Ctx(s), at(MON, 18))
        from app.brief import _coach
        assert _coach(s, MON, TZ) == []                                       # nothing in the brief either


# ---------------------------------------------------------------- streaks

def test_streaks(s):
    prefs_plan = {"week": MON.isoformat(), "sessions": [{"day": d, "focus": "x", "minutes": 20, "exercises": []}
                                                         for d in ("2026-10-20", "2026-10-22", "2026-10-24")]}
    s.add(SettingKV(key=coach.PLAN_KEY.format(MON.isoformat()), value=json.dumps(prefs_plan)))
    s.commit()
    for d in ("2026-10-20", "2026-10-22"):
        coach.log_entry(s, module="training", what="done", day=dt.date.fromisoformat(d))
    assert coach.streak(s, "training", dt.date(2026, 10, 24)) == 2          # today's not done yet: still 2
    coach.log_entry(s, module="training", what="done", day=dt.date(2026, 10, 24))
    assert coach.streak(s, "training", dt.date(2026, 10, 24)) == 3
    for i in (0, 1, 2, 4):
        coach.log_entry(s, module="faith", what="prayer", day=MON + dt.timedelta(days=i))
    assert coach.streak(s, "faith", MON + dt.timedelta(days=2)) == 3 and coach.streak(s, "faith", MON + dt.timedelta(days=3)) == 3
    assert coach.streak(s, "faith", MON + dt.timedelta(days=5)) == 1


# ---------------------------------------------------------------- the nudge rules

def run_nudges(cfg, now, **kw):
    base = {"plan": {}, "logged": [], "water_sent": 0, "study": [], "quiet": False}
    return coach.nudges(prefs.Coach.model_validate(cfg), now, **{**base, **kw})


def test_food_check_only_before_work_days_at_the_set_time():
    assert [n.ref for n in run_nudges(CFG, at(MON, 20))] == ["food:2026-10-19"]              # Tuesday is a work day
    assert run_nudges(CFG, at(MON, 19, 45)) == []                                           # not yet
    assert not any(n.ref.startswith("food") for n in run_nudges(CFG, at(MON + dt.timedelta(days=4), 20)))   # Friday: Saturday off
    done = CoachLog(module="food", day=MON, what="packed")
    assert run_nudges(CFG, at(MON, 21), logged=[done]) == []
    assert run_nudges({**CFG, "food_check": None}, at(MON, 20)) == []
    assert run_nudges(CFG, at(MON, 20), quiet=True) == []


def test_water_twice_a_day_in_work_hours_only_with_a_goal():
    assert coach.water_slots(prefs.Coach.model_validate(CFG)) == [dt.time(11), dt.time(14)]
    assert run_nudges(CFG, at(MON, 10, 59)) == []
    first = run_nudges(CFG, at(MON, 11))
    assert [(n.kind, n.ref) for n in first] == [("push", "water:2026-10-19:0")] and "0.0 of 2.0 l" in first[0].what
    assert run_nudges(CFG, at(MON, 13), water_sent=1) == []
    assert run_nudges(CFG, at(MON, 14), water_sent=1)[0].ref == "water:2026-10-19:1"
    assert run_nudges(CFG, at(MON, 16), water_sent=2) == []                                 # at most 2
    assert run_nudges(CFG, at(MON, 18), water_sent=1) == []                                 # after work
    assert run_nudges(CFG, at(MON + dt.timedelta(days=5), 11)) == []                        # Saturday
    drank = [CoachLog(module="water", day=MON, what="bottles", value=2.0)]
    assert run_nudges(CFG, at(MON, 11), logged=drank) == []                                 # goal reached
    assert run_nudges({**CFG, "water_l": None}, at(MON, 11)) == []


def test_skipped_sessions_get_pushback_leg_day_firmer():
    tue = {"day": "2026-10-20", "focus": "legs", "minutes": 25, "exercises": []}
    sat = {"day": "2026-10-24", "focus": "upper", "minutes": 20, "exercises": []}
    wed7 = at(MON + dt.timedelta(days=2), 7)
    leg = run_nudges(CFG, wed7, plan={MON + dt.timedelta(days=1): tue})
    assert [n.ref for n in leg] == ["training:missed:2026-10-20"] and "Leg day skipped" in leg[0].what and "no excuses" in leg[0].what
    other = run_nudges(CFG, at(dt.date(2026, 10, 25), 7), plan={dt.date(2026, 10, 24): sat})
    assert other[0].what.startswith("Yesterday's 20 min upper didn't happen")
    skipped_on_purpose = CoachLog(module="training", day=MON + dt.timedelta(days=1), what="sick", done=False)
    assert run_nudges(CFG, wed7, plan={MON + dt.timedelta(days=1): tue}, logged=[skipped_on_purpose]) == []


def test_study_and_snack_nudges():
    study = [{"subject": "Physics", "topic": "Energy", "minutes": 45, "event": 1}]
    assert run_nudges(CFG, at(MON, 17, 15), study=study)[0].what == "Study today: Physics, Energy (45 min). Commute: flashcards."
    assert not any(n.ref.startswith("study") for n in run_nudges(CFG, at(MON, 12), study=study))      # not before 17:00
    over = run_nudges({**CFG, "water_l": None, "food_check": None}, at(MON, 12), snacks_week=34.5)
    assert [n.ref for n in over] == ["snacks:2026-W43"] and "34 CHF, budget 20" in over[0].what


def test_snacks_are_their_own_category():
    assert life.categorize("K Kiosk Bern HB") == "snacks" and life.categorize("Coop Pronto Wankdorf") == "snacks"
    assert life.categorize("Coop Bern") == "groceries" and life.categorize("Migrolino") == "snacks"


# ---------------------------------------------------------------- study plans

def test_study_plan_to_the_exam_date(s, migrated):
    ev = SchoolEvent(subject="Physics", kind="exam", date=at(MON + dt.timedelta(days=30), 8))
    s.add(ev)
    s.commit()
    s.refresh(ev)
    assert coach.exams_needing_plans(s, MON, TZ) == [ev]
    cfg = prefs.coach(s)
    ask = lambda p, sys_, purpose=None: AskResult(json.dumps({"weeks": ["Mechanics", "Energy", "Waves", "Optics", "Revision"]}))  # noqa: E731
    p = coach.make_study_plan(ask, s, cfg=cfg, event=ev, today=MON, tz=TZ, language="English", notes="")
    days = [dt.date.fromisoformat(d) for w in p["weeks"] for d in w["days"]]
    assert all(coach.day_name(d) not in ("mon", "thu") for d in days) and max(days) < MON + dt.timedelta(days=30)
    assert all(len(w["days"]) == 3 for w in p["weeks"][1:-1]) and p["weeks"][0]["topic"] == "Mechanics"
    rel = coach.save_study_plan(s, migrated.data_dir / "vault", p)
    assert coach.exams_needing_plans(s, MON, TZ) == []
    assert "Commute: flashcards" in (migrated.data_dir / "vault" / rel).read_text(encoding="utf-8")
    assert coach.study_today(s, days[0])[0]["topic"] == "Mechanics"


# ---------------------------------------------------------------- tools, chat routing, brief, review

def test_the_coach_tools(setup):
    with Session(setup["engine"]) as s:
        prefs.set_key(s, "coach", CFG)
    r = json.loads(setup["call"]("coach_log", {"module": "water", "what": "bottle", "value": 0.75}).json()["text"])
    assert r["logged"]["module"] == "water" and r["water_today_l"] == 0.75
    r = json.loads(setup["call"]("coach_log", {"module": "faith", "what": "prayer after coffee"}).json()["text"])
    assert r["streak"] == 1
    assert setup["call"]("coach_log", {"module": "training", "what": "x", "day": "2020-01-01"}).json()["is_error"]
    st = json.loads(setup["call"]("coach_status", {}).json()["text"])
    assert st["faith"]["habit"] == "a short prayer" and st["water"]["today_l"] == 0.75 and st["settings"]["home_minutes"] == 25


def test_money_questions_go_to_opus():
    from app import models
    for q in ("Should I invest in an ETF?", "How much did I spend on snacks?", "Wie viel kann ich diesen Monat sparen?",
              "Was mache ich mit meinem Lohn?"):
        assert coach.purpose_for(q) == "money" and models.pick(coach.purpose_for(q)) == "opus", q
    for q in ("When is training?", "Wann ist Training?", "Save this to my profile", "Spargel kaufen"):
        assert coach.purpose_for(q) is None, q


def test_brief_and_review_show_the_coach(s):
    lines = coach.brief_lines(s, MON, TZ)
    assert lines == ["Training day.", "Faith: a short prayer, right after coffee."]
    assert coach.brief_lines(s, MON, TZ, cfg=prefs.coach(s).model_copy(update={"faith_habit": None})) == ["Training day."]
    from app.reviews import prefill
    s.add(SettingKV(key=coach.PLAN_KEY.format(MON.isoformat()), value=json.dumps(
        {"week": MON.isoformat(), "sessions": [{"day": "2026-10-20", "focus": "legs", "minutes": 25, "exercises": []}]})))
    s.commit()
    coach.log_entry(s, module="faith", what="prayer", day=MON)
    facts = prefill(s, "weekly", MON, MON + dt.timedelta(days=6), TZ)["facts"]
    assert "Home training: 0 of 1 sessions; skipped: Tue legs" in facts and "Faith habit on 1 of 7 days" in facts


# ---------------------------------------------------------------- money, people, style (pa.py)

def test_christmas_from_november_with_wishlists_and_budget(migrated):
    engine, p_, fake, vault_dir = make(migrated)
    with Session(engine) as s:
        prefs.set_key(s, "coach", {"gifts_year_chf": 800})
        mum = Person(name="Mum", relation="mother")
        s.add(mum)
        s.commit()
        s.refresh(mum)
        s.add(WishItem(person_id=mum.id, title="Cookbook", price_chf=35))
        s.commit()
        assert [p_.christmas_due(dt.date(2026, m, d)) for m, d in ((10, 31), (11, 1), (12, 24), (12, 25))] == [False, True, True, False]
        plan = p_.christmas_plan(s)
        assert plan["people"][0]["wishlist"][0]["title"] == "Cookbook" and plan["per_person_chf"] == 400
        ctx = Ctx(s, {"ideas": [{"idea": "Cookbook", "why": "on her wishlist"}]})
        assert p_._christmas(ctx, dt.datetime(2026, 10, 31, 10, tzinfo=TZ)) == 0
        assert p_._christmas(ctx, dt.datetime(2026, 11, 1, 10, tzinfo=TZ)) == 1
        assert "- Cookbook, CHF 35" in ctx.prompts[0][1] and "Budget about 400 CHF" in ctx.prompts[0][1]
        loop = s.exec(select(OpenLoop).where(OpenLoop.source == "christmas")).one()
        assert "400 CHF a person" in json.loads(loop.draft_json)["note"]


def test_partner_date_ideas_and_family_time(migrated):
    engine, p_, fake, vault_dir = make(migrated)
    with Session(engine) as s:
        s.add(Person(name="Alex", relation="partner", notes="Loves sushi and hiking"))
        s.add(Person(name="Dad", relation="father", last_contact=dt.date(2026, 9, 1)))
        s.commit()
        ctx = Ctx(s, {"text": "Lunch Saturday?"}, {}, {"ideas": [{"idea": "Sushi night", "why": "loves sushi"}]})   # Dad + polish, then the date
        p_.people(ctx, dt.datetime(2026, 10, 14, 10, tzinfo=TZ))
        date = s.exec(select(OpenLoop).where(OpenLoop.source == "dates")).one()
        assert date.what == "Date idea for you and Alex: 3 ideas are ready." and "Sushi" in date.draft_json
        dad = s.exec(select(OpenLoop).where(OpenLoop.who == "Dad")).one()
        assert dad.what.startswith("No 1:1 time with Dad in 6 weeks")
        p_.people(Ctx(s), dt.datetime(2026, 10, 15, 10, tzinfo=TZ))
        assert len(s.exec(select(OpenLoop).where(OpenLoop.source == "dates")).all()) == 1   # once per two weeks


# ---------------------------------------------------------------- acceptance 24-28

class Coachy:
    """The pieces the acceptance tests use from the live app, with a fake model."""

    def __init__(self, migrated, answers: dict):
        engine, self.pa, fake, self.vault_dir = make(migrated)
        self.coach = coach.Coacher(engine=engine, profile=PROFILE, schedule={}, vault_dir=self.vault_dir)
        self.answers, self.asked = answers, []

    def ask(self, prompt, system, purpose="summary", images=None):
        self.asked.append(purpose)
        return AskResult(json.dumps(self.answers.get(purpose, {})))


def actx(migrated, c: Coachy, turn=None):
    ctx = ctx_for(migrated, turn=turn)
    ctx.ask, ctx.profile, ctx.coach, ctx.pa, ctx.vault_dir = c.ask, PROFILE, c.coach, c.pa, c.vault_dir
    return ctx


def logging_turn(migrated, reply="Logged: 20 min core. Streak: 3 in a row."):
    async def turn(text, session=None):
        with Session(make_engine(migrated.db_url)) as s:
            coach.log_entry(s, module="training", what="20 min core", day=dt.datetime.now(TZ).date())
        return Turn(reply=reply)
    return turn


def next_week_plan():
    start = coach.week_start(dt.datetime.now(TZ).date()) + dt.timedelta(days=7)
    days = [start + dt.timedelta(days=i) for i in (1, 5)]                      # Tue, Sat
    return {"sessions": [{"day": days[0].isoformat(), "focus": "legs", "minutes": 25,
                          "exercises": [{"name": "Goblet squat", "dose": "4x10", "equipment": "kettlebell"}]},
                         {"day": days[1].isoformat(), "focus": "upper", "minutes": 20,
                          "exercises": [{"name": "Push-ups", "dose": "3x12", "equipment": "bodyweight"}]}]}


def test_acceptance_24_training(migrated, s):
    c = Coachy(migrated, {"coach": next_week_plan()})
    r = asyncio.run(acceptance.t_training(actx(migrated, c, turn=logging_turn(migrated))))
    assert r["ok"], r
    assert s.exec(select(CoachLog)).all() == []                                          # cleaned up
    c = Coachy(migrated, {"coach": {"sessions": [next_week_plan()["sessions"][1]]}})       # one session, no leg day
    r = asyncio.run(acceptance.t_training(actx(migrated, c, turn=logging_turn(migrated))))
    assert not r["ok"] and r["checks"]["a leg day"] is False and r["checks"]["the set number of home sessions"] is False
    c = Coachy(migrated, {"coach": next_week_plan()})
    r = asyncio.run(acceptance.t_training(actx(migrated, c, turn=logging_turn(migrated, reply="Nice work today!"))))
    assert not r["ok"] and r["checks"]["one line with the streak"] is False


def test_acceptance_24_needs_coach_settings(migrated):
    c = Coachy(migrated, {})
    r = asyncio.run(acceptance.t_training(actx(migrated, c)))
    assert not r["ok"] and "No coach settings yet" in r["detail"]


def test_acceptance_25_food_and_water(migrated, s):
    r = asyncio.run(acceptance.t_food_water(actx(migrated, Coachy(migrated, {}))))
    assert r["ok"], r
    assert "food check 20:00" in r["detail"] and "water goal 2.0" in r["detail"]


def test_acceptance_26_study(migrated, s):
    c = Coachy(migrated, {"study": {"weeks": ["Mechanics", "Energy", "Waves", "Optics", "Revision"]}})
    r = asyncio.run(acceptance.t_study(actx(migrated, c)))
    assert r["ok"], r
    assert s.exec(select(SchoolEvent)).all() == [] and c.asked == ["study"]


def test_acceptance_27_money(migrated, s):
    good = {"money_review": {"facts": ["Snacks 95 CHF, budget 60."], "options": ["Pack snacks", "Set aside 100 CHF"],
                             "recommendation": "Pack snacks: saves about 30 CHF."}}
    c = Coachy(migrated, good)
    ctx = actx(migrated, c)

    class Gate:
        actions = {"finance.list": 0, "gmail.send": 3}
    ctx.gate = Gate()
    from app.db import ModelRun
    s.add(ModelRun(purpose="money_review", model="claude-opus-4", ts=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)))
    s.commit()
    r = asyncio.run(acceptance.t_money(ctx))
    assert r["ok"], r
    assert s.exec(select(Person)).all() == [] and c.asked == ["money_review"]
    c = Coachy(migrated, {"money_review": {"facts": [], "options": ["x"], "recommendation": ""}})
    ctx = actx(migrated, c)
    Gate.actions = {"finance.list": 0, "finance.pay": 5}
    ctx.gate = Gate()
    r = asyncio.run(acceptance.t_money(ctx))
    assert not r["ok"] and r["checks"]["no tool can move money"] is False


def test_acceptance_28_people_faith_style(migrated, s):
    c = Coachy(migrated, {"draft": {"ideas": [{"idea": "The climbing chalk bag from the wishlist"}]}})
    r = asyncio.run(acceptance.t_people_faith_style(actx(migrated, c)))
    assert r["ok"], r
    assert s.exec(select(Person)).all() == []
    c = Coachy(migrated, {"draft": {"ideas": [{"idea": "A scented candle"}]}})
    r = asyncio.run(acceptance.t_people_faith_style(actx(migrated, c)))
    assert not r["ok"] and r["checks"]["gift ideas start from the wishlist"] is False



def test_the_today_card(migrated):
    from fastapi.testclient import TestClient

    from app.main import create_app
    with TestClient(create_app(migrated)) as c:
        assert c.get("/api/coach").json()["on"] is False
        assert c.put("/api/settings/coach", json={"value": {"water_l": 2, "faith_habit": "a short prayer"}}).status_code == 200
        st = c.get("/api/coach").json()
        assert st["on"] is True and st["water"]["goal_l"] == 2 and st["faith"]["habit"] == "a short prayer"
        r = c.post("/api/coach/log", json={"module": "water", "what": "glass", "value": 0.3}).json()
        assert r["ok"] and r["water_today_l"] == 0.3
        assert c.post("/api/coach/log", json={"module": "money", "what": "x"}).status_code == 400
        assert c.post("/api/coach/log", json={"module": "faith", "what": "x", "day": "2020-01-01"}).status_code == 400
        assert c.post("/api/coach/log", json={"module": "faith", "what": "prayer"}).json()["streak"] == 1
