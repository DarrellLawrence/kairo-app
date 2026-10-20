"""Owner's request 2026-10-20: a personal to-do (only the owner does it) keeps coming back, but at a good
moment (the evening at home; never on the commute, never during a call, never in quiet hours: deferred,
not dropped), with one concrete next step and how long it takes (made once by the cheap model and kept on
the loop), and each skip makes the next one sooner and more direct, then a smaller first step."""

from __future__ import annotations

import datetime as dt
import json
import types

import pytest
from sqlmodel import Session

from app import brief, loops
from app.calendar_read import CalendarRead
from app.chat import AskResult
from app.db import LocationEvent, OpenLoop
from app.settings import REPO_ROOT
from tests.test_loops import PROFILE, TIMES, TZ, WED, Pushes, env  # noqa: F401  (env: the loops fixture)

STEP = {"step": "open the shop's site and download the card template", "minutes": 5,
        "smaller": "write down two things Sam likes", "smaller_minutes": 2}
EVENING = WED.replace(hour=18)                    # Wednesday 18:00, after the after-work good time (17:30)


class Ctx:
    """The cheap model for the next step (purpose 'reminder'); every other purpose is not expected here."""

    def __init__(self, s, step=STEP, down=False):
        self.session, self.step, self.down, self.prompts = s, step, down, []

    def ask(self, prompt, system, images=None, purpose=None):
        self.prompts.append((purpose, prompt))
        if self.down:
            return AskResult("", status="error", error="model unavailable")
        return AskResult(json.dumps(self.step))

    def steps_asked(self):
        return [p for purpose, p in self.prompts if purpose == "reminder"]


def engine_for(env, pushes, on_call=lambda: False):  # noqa: F811
    from app import loop_check
    cal = lambda day: CalendarRead(ok=True, events=[], days=7)  # noqa: E731
    return loops.LoopEngine(engine=env["engine"], profile=PROFILE, schedule={}, google_read=env["read"], calendar=cal,
                            pusher=pushes, vault_dir=env["vault"], config_dir=REPO_ROOT / "config", on_call=on_call,
                            checker=loop_check.LoopChecker(profile=PROFILE, google_read=env["read"], calendar=cal))


def todo(s, *, due=None, what="Buy a birthday present for Sam", kind="task", now=WED) -> OpenLoop:
    """The owner's own to-do (said in chat: no check needed), due at once."""
    loop = loops.add(s, kind=kind, what=what, who=None, due=due, now=now, tz=TZ, times=TIMES)
    loop.next_at = now
    s.add(loop)
    s.commit()
    return loop


def at_place(s, place, event, when):
    s.add(LocationEvent(place=place, event=event, ts=when.astimezone(dt.timezone.utc)))
    s.commit()


def run(env, s, ctx, now, pushes=None, on_call=lambda: False):  # noqa: F811
    return json.loads(engine_for(env, pushes if pushes is not None else Pushes(), on_call).run(ctx, now=now))


def test_a_personal_todo_comes_in_the_evening_at_home_with_one_next_step(env):  # noqa: F811
    pushes = Pushes()
    with Session(env["engine"]) as s:
        loop = todo(s, due=dt.date(2026, 10, 17))                       # Saturday: normal, can wait for the evening
        ctx = Ctx(s)
        out = run(env, s, ctx, WED, pushes)                             # Wednesday 10:00: not a good moment
        assert out["raised"] is None and "good moment" in out["why"] and pushes.sent == []
        at_place(s, "Home", "arrive", EVENING - dt.timedelta(minutes=10))
        out = run(env, s, ctx, EVENING, pushes)
        assert out["raised"] == loop.id
        assert pushes.sent == [("Kairo", "Buy a birthday present for Sam, due Sat. Next step: open the shop's site and "
                                         "download the card template, 5 min.", f"/#loops/{loop.id}")]
        [asked] = ctx.steps_asked()                                     # the cheap model, once
        assert "UNTRUSTED DATA" in asked and "Buy a birthday present for Sam" in asked
        s.refresh(loop)
        assert loops.step_of(loop)["text"] == STEP["step"]              # kept on the loop
        nxt = loops.aware(loop.next_at).astimezone(TZ)
        assert (nxt.date(), nxt.strftime("%H:%M")) == (dt.date(2026, 10, 15), "17:30")   # at most once a day
        assert run(env, s, ctx, EVENING + dt.timedelta(hours=2), pushes)["raised"] is None
        thu = EVENING + dt.timedelta(days=1)
        assert run(env, s, ctx, thu, pushes)["raised"] == loop.id
        assert pushes.sent[-1][1] == ("Buy a birthday present for Sam, due Sat. Still not done. Next step: open the shop's "
                                      "site and download the card template, 5 min.")   # more direct
        assert len(ctx.steps_asked()) == 1                              # never made again for the next reminder


def test_never_on_the_commute_nor_during_a_call_deferred_not_dropped(env):  # noqa: F811
    pushes = Pushes()
    with Session(env["engine"]) as s:
        loop = todo(s, due=dt.date(2026, 10, 17))
        loops.mark_raised(s, loop, WED - dt.timedelta(days=1), TZ, TIMES)   # as if raised yesterday: due again tonight
        loop.next_at = WED
        s.add(loop)
        s.commit()
        ctx = Ctx(s)
        at_place(s, "Home", "leave", EVENING - dt.timedelta(minutes=10))
        out = run(env, s, ctx, EVENING, pushes)
        assert out["raised"] is None and "on the way" in out["why"]
        at_place(s, "Gym", "arrive", EVENING + dt.timedelta(minutes=20))
        assert "not at home" in run(env, s, ctx, EVENING + dt.timedelta(minutes=30), pushes)["why"]
        at_place(s, "Home", "arrive", EVENING + dt.timedelta(hours=1))
        on_call = run(env, s, ctx, EVENING + dt.timedelta(hours=1, minutes=5), pushes, on_call=lambda: True)
        assert on_call["raised"] is None and "on a call" in on_call["why"]
        assert pushes.sent == [] and s.get(OpenLoop, loop.id).status == "open"   # waiting, never dropped
        assert run(env, s, ctx, EVENING + dt.timedelta(hours=1, minutes=20), pushes)["raised"] == loop.id
        late = WED.replace(hour=23, minute=30)                          # quiet hours: nothing at all
        s.get(OpenLoop, loop.id).next_at = late
        s.commit()
        with pytest.raises(loops.Skipped):
            run(env, s, ctx, late, pushes)


def test_without_location_the_evening_alone_decides(env):  # noqa: F811
    with Session(env["engine"]) as s:
        loop = todo(s, due=dt.date(2026, 10, 17))
        ctx = Ctx(s)
        assert run(env, s, ctx, WED.replace(hour=16))["raised"] is None
        assert run(env, s, ctx, EVENING)["raised"] == loop.id


def test_each_skip_comes_back_sooner_and_more_direct_then_a_smaller_first_step(env):  # noqa: F811
    with Session(env["engine"]) as s:
        loop = todo(s)                                                  # no date: low
        assert loop.urgency == "low"
        gaps = [(loops.come_back(loop, step, EVENING, TZ, TIMES).date() - EVENING.date()).days for step in range(5)]
        assert gaps == [3, 2, 1, 1, 1]                                  # sooner each time, never more than once a day
        assert all(loops.come_back(loop, k, EVENING, TZ, TIMES).astimezone(TZ).strftime("%H:%M") in ("17:30", "10:00")
                   for k in range(5))                                   # a good time, never in quiet hours
        loops.set_step(s, loop, STEP)
        said = []
        for skips in range(5):
            loop.snoozed = skips
            said.append(loops.phrase(loop, EVENING, TZ))
        assert said[0] == "Buy a birthday present for Sam. Next step: open the shop's site and download the card template, 5 min."
        assert said[1].startswith("Buy a birthday present for Sam. Still not done. Next step:")
        assert said[2] == ("Buy a birthday present for Sam: you've put it off 2 times. Just do the next step now: open the "
                           "shop's site and download the card template, 5 min.")
        assert said[3] == ("Buy a birthday present for Sam: you've put it off 3 times. Start smaller: write down two things "
                           "Sam likes, 2 min. That's all.")
        assert "put it off 4 times. Start smaller" in said[4]
        loop.snoozed = 1                                                # Later from the app follows the same ladder
        loop, msg = loops.snooze(s, loop.id, now=EVENING, tz=TZ, times=TIMES, when="later")
        assert (loops.aware(loop.next_at).astimezone(TZ).date() - EVENING.date()).days == 2 and loop.snoozed == 2


def test_overdue_ranks_urgent_but_waits_for_the_evening_and_due_today_does_not(env):  # noqa: F811
    pushes = Pushes()
    with Session(env["engine"]) as s:
        late = todo(s, due=dt.date(2026, 10, 9), what="Return the library books")       # overdue since Fri
        assert late.urgency == "urgent" and loops.open_loops(s)[0].id == late.id
        assert run(env, s, Ctx(s), WED, pushes)["raised"] is None                     # 10:00: it can't be fixed by noon
        today = todo(s, due=WED.date(), what="Sign the school form", now=WED + dt.timedelta(seconds=1))   # due today
        out = run(env, s, Ctx(s), WED + dt.timedelta(minutes=1), pushes)
        assert out["raised"] == today.id and pushes.sent[-1][1].startswith("Sign the school form, due today.")
        s.refresh(today)
        assert loops.aware(today.next_at) == WED + dt.timedelta(minutes=31)           # the urgent rules, as before
        assert run(env, s, Ctx(s), EVENING, pushes)["raised"] == late.id                # the evening: the overdue one
        assert pushes.sent[-1][1].startswith("Return the library books, overdue since Fri. Next step:")
        s.refresh(late)
        assert (loops.aware(late.next_at).astimezone(TZ).date() - EVENING.date()).days == 1   # once a day, not hourly


def test_the_brief_mentioning_it_does_not_eat_the_evening_reminder(env):  # noqa: F811
    with Session(env["engine"]) as s:
        loop = todo(s, due=dt.date(2026, 10, 9), what="Return the library books")
        before = (loops.aware(loop.next_at), loop.snoozed)
        maker = brief.BriefMaker(engine=env["engine"], google_read=None, vault_dir=env["vault"], profile=PROFILE)
        maker._mark_loops_raised(s, [{"id": loop.id}], types.SimpleNamespace(jobs=types.SimpleNamespace(schedule={})))
        s.refresh(loop)
        assert (loops.aware(loop.next_at), loop.snoozed) == before


def test_no_model_no_step_but_the_reminder_still_comes_and_the_step_is_asked_for_next_time(env):  # noqa: F811
    pushes = Pushes()
    with Session(env["engine"]) as s:
        loop = todo(s, due=dt.date(2026, 10, 17))
        down = Ctx(s, down=True)
        assert run(env, s, down, EVENING, pushes)["raised"] == loop.id
        assert pushes.sent[-1][1] == "Buy a birthday present for Sam, due Sat."
        assert len(down.steps_asked()) == 1 and loops.step_of(s.get(OpenLoop, loop.id)) is None
        ok = Ctx(s)
        assert run(env, s, ok, EVENING + dt.timedelta(days=1), pushes)["raised"] == loop.id
        assert "Next step: open the shop's site" in pushes.sent[-1][1] and len(ok.steps_asked()) == 1


def test_a_reply_someone_waits_for_is_not_held_for_the_evening(env):  # noqa: F811
    """Only the owner's own to-dos wait for a good moment; a person waiting still gets the ping at once."""
    from tests.test_loops import DRAFT, Ctx as LoopCtx, triaged
    pushes = Pushes()
    with Session(env["engine"]) as s:
        triaged(s, days_ago=0)
        out = run(env, s, LoopCtx(s, DRAFT), WED, pushes)
        assert out["raised"] is not None and "Reply" in pushes.sent[0][1]
