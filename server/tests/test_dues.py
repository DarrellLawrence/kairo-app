"""Owner's request 2026-10-20: a deadline that already passed never reads "due Fri". One wording for a due
date everywhere Kairo says one (Today, the Today card, the loops, the brief, the voice agenda, the tools,
the recap), by the owner's calendar day; overdue ranks as urgent."""

from __future__ import annotations

import datetime as dt
import json
import types
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import Session

from app import bringup, brief, dues, loops, recap, today, todaycard, tools
from app.chat import AskResult
from app.db import Task, make_engine
from tests.test_jobs import Model, brief_job, env, seed_day  # noqa: F401  (env: the brief's fixture)
from tests.test_pa import PROFILE

TZ = ZoneInfo("Europe/Zurich")
TUE = dt.datetime(2026, 10, 20, 8, 0, tzinfo=TZ)          # Tuesday morning
TIMES = loops.good_times({})


def at(day: dt.date, hour: int = 12, tz=TZ) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour), tzinfo=tz)


def test_one_wording_by_the_owners_calendar_day():
    w = lambda due, now=TUE: dues.words(due, now, TZ)  # noqa: E731
    assert w(at(dt.date(2026, 10, 19))) == "overdue since yesterday"
    assert w(at(dt.date(2026, 10, 16))) == "overdue since Fri"            # not "due Fri"
    assert w(at(dt.date(2026, 10, 11))) == "overdue 9 days"
    assert w(at(dt.date(2026, 10, 20))) == "due today"
    assert w(at(dt.date(2026, 10, 21))) == "due tomorrow"
    assert w(at(dt.date(2026, 10, 26))) == "due Mon"                       # within 6 days: the weekday
    assert w(at(dt.date(2026, 10, 27))) == "due 27 Oct"                    # a week or more: the date
    assert w(at(dt.date(2026, 11, 3))) == "due 3 Nov"


def test_a_date_only_due_is_today_all_day_and_overdue_only_from_the_next_day():
    noon = at(TUE.date())                                                  # a date alone is stored as noon
    for hour in (6, 11, 13, 22):
        assert dues.words(noon, TUE.replace(hour=hour), TZ) == "due today"
        assert not dues.overdue(noon, TUE.replace(hour=hour), TZ)
    assert dues.words(noon, TUE + dt.timedelta(days=1), TZ) == "overdue since yesterday"


def test_the_owners_timezone_decides_the_day_not_utc():
    assert dues.words(dt.datetime(2026, 10, 19, 22, 30, tzinfo=dt.timezone.utc), TUE, TZ) == "due today"   # 00:30 Tue here
    nz = ZoneInfo("Pacific/Auckland")
    noon = at(dt.date(2026, 10, 20), tz=nz).astimezone(dt.timezone.utc)   # the day before in UTC
    morning = dt.datetime(2026, 10, 20, 7, 0, tzinfo=nz)
    assert noon.date() == dt.date(2026, 10, 19)
    assert dues.words(noon, morning, nz) == "due today" and not dues.overdue(noon, morning, nz)
    naive_utc = dt.datetime(2026, 10, 16, 10, 0)                           # SQLite hands back naive UTC
    assert dues.words(naive_utc, TUE, TZ) == "overdue since Fri"


def test_today_top3_words_dues_with_the_helper_and_flags_overdue(migrated):
    with Session(make_engine(migrated.db_url)) as s:
        s.add(Task(title="Hand in the form", due=at(dt.date(2026, 10, 16))))
        s.add(Task(title="Order the cake", due=at(dt.date(2026, 10, 28))))
        s.add(Task(title="Call the garage", due=at(dt.date(2026, 10, 20))))
        s.commit()
        top = today.top3(s, TUE.date(), TZ)
    assert [(t["title"], t["why"], t["overdue"]) for t in top] == [
        ("Hand in the form", "overdue since Fri", True), ("Call the garage", "due today", False),
        ("Order the cake", "due 28 Oct", False)]                         # not "due Wed": that's tomorrow's Wednesday


def test_the_today_card_never_says_due_fri_for_a_friday_that_passed():
    lp = types.SimpleNamespace(kind="task", what="buy a birthday present for Sam", who=None, urgency="urgent",
                               due=at(dt.date(2026, 10, 16), 18))
    soon = types.SimpleNamespace(kind="task", what="book the dentist", who=None, urgency="normal",
                                 due=at(dt.date(2026, 10, 23), 18))
    lines = todaycard.loops_lines([lp, soon], TZ, now=TUE).splitlines()
    assert lines == ["- task: buy a birthday present for Sam, overdue since Fri [urgent]", "- task: book the dentist, due Fri"]
    t = lambda title, due: types.SimpleNamespace(title=title, due=due)  # noqa: E731
    rows = [t("form", at(dt.date(2026, 10, 19), 14)), t("call the garage", at(TUE.date())), t("bike lock", TUE.replace(hour=17))]
    assert todaycard.tasks_lines(rows, TUE).splitlines() == [
        "- form (overdue since yesterday)", "- call the garage", "- bike lock by 17:00"]   # a date alone has no "by 12:00"


def test_loop_phrases_and_the_voice_agenda_use_the_helper(migrated):
    with Session(make_engine(migrated.db_url)) as s:
        lp, _ = loops.upsert(s, kind="task", source="profile", ref="p1", what="Buy a birthday present for Sam",
                             due=at(dt.date(2026, 10, 16), 18), now=TUE, tz=TZ, times=TIMES)
        assert lp.urgency == "urgent"                                      # overdue ranks as urgent
        assert loops.phrase(lp, TUE, TZ) == "Buy a birthday present for Sam, overdue since Fri."
        assert loops.as_dict(lp, TUE, TZ)["when"] == "overdue since Fri"
        from app import loop_check
        loop_check.passed(s, lp)
        [item] = [i for i in bringup.candidates(s, PROFILE, TUE) if i.kind == "loop"]
        assert item.priority == "urgent" and item.text == "task: Buy a birthday present for Sam (overdue since Fri)"


def test_the_tools_tell_the_model_how_to_word_the_due():
    t = Task(id=1, title="Hand in the form", due=at(dt.date(2026, 10, 16)))
    assert tools._task(t, TZ, now=TUE)["when"] == "overdue since Fri"
    assert tools._task(Task(id=2, title="Someday"), TZ, now=TUE)["when"] is None


def test_the_plain_brief_words_dues_with_the_helper(env):  # noqa: F811
    seed_day(env)                                                          # "Old thing" was due Sat 10th, the brief is for Wed 14th
    jobs = brief_job(env, Model(AskResult("", "logged_out", "Claude is logged out")))
    jobs.run_job("morning_brief", forced=True, day=dt.date(2026, 10, 14))
    with Session(env["engine"]) as s:
        b = brief.latest(s, dt.date(2026, 10, 14))
    assert b["overdue"][0]["when"] == "overdue since Sat" and b["next7"][0]["when"] == "due tomorrow"
    assert [(t["title"], t["why"]) for t in b["top3"]][:2] == [("Old thing", "overdue since Sat"), ("Lernen Mathe", "due tomorrow")]


def test_the_evening_recap_never_calls_todays_date_only_task_overdue(migrated):
    evening = TUE.replace(hour=20, minute=30)
    with Session(make_engine(migrated.db_url)) as s:
        s.add(Task(title="Call the garage", due=at(TUE.date())))           # date only: noon, still today
        s.add(Task(title="Pay the phone bill", due=at(dt.date(2026, 10, 19))))
        s.commit()
        r = recap.Recap(engine=None, gate=None, executor=None, tz=TZ, calendar=lambda day: ([], None))
        _, body = r.build(s, evening)
    assert "Pay the phone bill (overdue since yesterday)" in body
    assert "Call the garage (overdue" not in body


@pytest.mark.parametrize("when", ["later", "tomorrow"])
def test_an_overdue_loop_can_be_snoozed_and_never_comes_back_in_15_minutes(migrated, when):
    with Session(make_engine(migrated.db_url)) as s:
        lp, _ = loops.upsert(s, kind="deadline", source="profile", ref="p2", what="Renew the bike vignette",
                             due=at(dt.date(2026, 10, 16), 18), now=TUE, tz=TZ, times=TIMES)
        lp.snoozed = 2
        s.add(lp)
        s.commit()
        lp, msg = loops.snooze(s, lp.id, now=TUE, tz=TZ, times=TIMES, when=when)   # before: "can't move, it's due Friday"
        assert loops.aware(lp.next_at) - TUE >= dt.timedelta(hours=1)          # not clamped to "15 min before the deadline"
        assert "due Fri" not in msg and "Friday" not in msg and "overdue since Fri" in msg
        assert json.dumps(loops.as_dict(lp, TUE, TZ))                       # still a normal loop for the app
