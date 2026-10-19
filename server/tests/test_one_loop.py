"""Fix plan item 18: one real thing = one loop. A mail that needs a reply and the task triage made
from it; a test and its study plan; a promise told in chat and found again in sent mail."""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session, select

from app import loops
from app.db import OpenLoop, SchoolEvent, Task, TriagedEmail, make_engine
from tests.test_loops import TIMES, TZ, WED, mail, triaged

SHOP, ORDER = "Shopco <hello@shopco.example>", "Is everything fine with your Shopco order?"


def open_(s, **where):
    q = select(OpenLoop).where(OpenLoop.status == "open")
    for k, v in where.items():
        q = q.where(getattr(OpenLoop, k) == v)
    return s.exec(q).all()


def mail_with_task(s, mid, sender=SHOP, subject=ORDER, summary="asks how the order went", personal=None):
    """A mail triage found a date in, and the task it made from it (as triage did before 2026-10-19)."""
    mail(s, mid, sender, subject, summary, personal=personal)
    t = Task(title=f"{subject} (from {sender.split('<')[0].strip()})", due=WED + dt.timedelta(days=2), priority=2,
             source="email")
    s.add(t)
    s.commit()
    s.refresh(t)
    m = s.get(TriagedEmail, mid)
    m.task_id, m.deadline = t.id, t.due.date()
    s.add(m)
    s.commit()
    return t


def test_a_shops_mail_with_a_date_is_never_a_loop(migrated):
    """2026-10-19: a shop's feedback mail became three identical 'deadline' loops. A task made from a mail never
    gets a loop of its own: the mail's reply loop is the one loop if a person waits, else there is none."""
    with Session(make_engine(migrated.db_url)) as s:
        mail_with_task(s, "s1", personal=False)                                        # the reply check: automated
        mail_with_task(s, "s2", personal=False)
        mail_with_task(s, "s3")                                                        # not checked yet
        loops.sync(s, now=WED, tz=TZ, times=TIMES)
        assert open_(s) == []


def test_a_persons_mail_with_a_date_is_one_loop_once_checked(migrated):
    with Session(make_engine(migrated.db_url)) as s:
        mail_with_task(s, "k1", sender="Mr. Keller <keller@school.example>", subject="Projektarbeit",
                       summary="wants the project by Friday")
        loops.sync(s, now=WED, tz=TZ, times=TIMES)
        assert open_(s) == []                                                          # not checked yet: no loop
        m = s.get(TriagedEmail, "k1")
        m.personal_reply = True
        s.add(m)
        s.commit()
        loops.sync(s, now=WED, tz=TZ, times=TIMES)
        [loop] = open_(s)
        assert (loop.source, loop.kind, loop.who_email) == ("email", "reply", "keller@school.example")
        assert loops.aware(loop.due).date() == (WED + dt.timedelta(days=2)).date()


def test_the_same_thing_from_the_same_sender_is_one_loop(migrated):
    with Session(make_engine(migrated.db_url)) as s:
        for mid in ("a1", "a2", "a3"):
            mail(s, mid, "Anna Meier <anna@example.com>", "Project review", "Asks if Thursday works", personal=True)
        mail(s, "a4", "Anna Meier <anna@example.com>", "Agenda", "Sends the agenda, asks for comments", personal=True)
        mail(s, "b1", "Tom Weber <tom@example.com>", "Project review", "Asks if Thursday works!", personal=True)
        loops.sync(s, now=WED, tz=TZ, times=TIMES)
        anna = [lp for lp in open_(s, who_email="anna@example.com") if lp.source_ref != "a4"]
        assert len(anna) == 1 and len(open_(s)) == 3                                  # a4 and Tom's are their own
        dups = [lp for lp in s.exec(select(OpenLoop).where(OpenLoop.source_ref.in_(["a1", "a2", "a3"]))).all()
                if lp.id != anna[0].id]
        assert [d.close_reason for d in dups] == [f"check: duplicate of #{anna[0].id}"] * 2
        assert loops.sync(s, now=WED, tz=TZ, times=TIMES)["new"] == 0
        loops.close(s, anna[0].id, "done", now=WED)
        loops.sync(s, now=WED, tz=TZ, times=TIMES)
        assert sorted(lp.source_ref for lp in open_(s)) == ["a4", "b1"]               # the duplicates don't come back


def test_a_mail_and_its_task_are_one_loop(migrated):
    with Session(make_engine(migrated.db_url)) as s:
        triaged(s, deadline=(WED + dt.timedelta(days=2)).date())
        t = Task(title="Reply to Anna about the project review", due=WED + dt.timedelta(days=2), source="email", priority=2)
        s.add(t)
        s.commit()
        s.refresh(t)
        m = s.get(TriagedEmail, "m1")
        m.task_id = t.id
        s.add(m)
        s.commit()
        loops.sync(s, now=WED, tz=TZ, times=TIMES)
        assert [(lp.source, lp.kind) for lp in open_(s)] == [("email", "reply")]          # not also a task loop
        s.add(Task(title="Buy a birthday card", due=WED + dt.timedelta(days=1)))          # a task of its own still gets one
        s.commit()
        loops.sync(s, now=WED, tz=TZ, times=TIMES)
        assert sorted(lp.source for lp in open_(s)) == ["email", "task"]


def test_a_test_and_its_study_plan_are_one_loop(migrated):
    with Session(make_engine(migrated.db_url)) as s:
        e = SchoolEvent(subject="Physics", kind="test", date=WED + dt.timedelta(days=3))
        s.add(e)
        s.commit()
        s.refresh(e)
        loops.sync(s, now=WED, tz=TZ, times=TIMES)
        [school] = open_(s, source="school")
        loops.upsert(s, kind="study", source="study", ref=str(e.id), what="Physics test Saturday: 3 study blocks planned.",
                     due=WED + dt.timedelta(days=3), now=WED, tz=TZ, times=TIMES)       # what pa.tests does
        loops.close(s, school.id, "merged", now=WED)
        loops.sync(s, now=WED + dt.timedelta(minutes=15), tz=TZ, times=TIMES)
        assert [(lp.source, lp.what) for lp in open_(s)] == [("study", "Physics test Saturday: 3 study blocks planned.")]


def test_pa_closes_the_school_loop_when_it_plans(migrated):
    from tests.test_loops import Ctx
    from tests.test_pa import make
    engine, p_, fake, vault_dir = make(migrated)
    with Session(engine) as s:
        e = SchoolEvent(subject="Physics", kind="test", date=WED + dt.timedelta(days=3))
        s.add(e)
        s.commit()
        loops.sync(s, now=WED, tz=TZ, times=TIMES)
        assert len(open_(s, source="school")) == 1
        p_.tests(Ctx(s, {"markdown": "# Guide"}, {"cards": []}), WED)
        assert open_(s, source="school") == [] and len(open_(s, source="study")) == 1


def test_a_promise_from_chat_and_from_mail_is_one_loop(migrated):
    with Session(make_engine(migrated.db_url)) as s:
        loops.add(s, kind="promise", what="Send Marc the slides", who="Marc", due=None, now=WED, tz=TZ, times=TIMES)
        loops.add(s, kind="promise", what="send Marc the slides.", who="marc", due=None, now=WED, tz=TZ, times=TIMES)
        assert len(open_(s, kind="promise")) == 1
        assert loops.promise_ref("Marc", "Send Marc the slides!") == loops.promise_ref("marc", "send marc the slides")
        loops.add(s, kind="task", what="Send Marc the slides", who=None, due=None, now=WED, tz=TZ, times=TIMES)
        assert len(open_(s)) == 2                                                     # a plain task stays its own


def test_coach_leaves_the_last_five_days_to_pa(migrated):
    import json

    from app import coach, prefs
    from app.db import SettingKV
    from tests.test_loops import Ctx, Pushes
    from tests.test_pa import PROFILE
    day = WED.date()
    near = {"event": 1, "subject": "Physics", "topic": "", "date": (day + dt.timedelta(days=3)).isoformat(), "minutes": 45,
            "weeks": [{"start": day.isoformat(), "topic": "Energy", "days": [day.isoformat()]}]}
    far = {**near, "event": 2, "subject": "History", "date": (day + dt.timedelta(days=20)).isoformat(),
           "weeks": [{"start": day.isoformat(), "topic": "1848", "days": [day.isoformat()]}]}
    with Session(make_engine(migrated.db_url)) as s:
        prefs.set_key(s, "coach", {"home_sessions": 0, "food_check": None})
        s.add(SettingKV(key="coach.study.1", value=json.dumps(near)))
        s.add(SettingKV(key="coach.study.2", value=json.dumps(far)))
        s.commit()
        ctx = Ctx(s)
        ctx.jobs = type("J", (), {"pusher": Pushes()})()
        coach.Coacher(engine=s.get_bind(), profile=PROFILE, schedule={}, vault_dir=migrated.data_dir / "vault").run(
            ctx, dt.datetime.combine(day, dt.time(17, 30), tzinfo=TZ))
        [study] = open_(s, source="coach")
        assert study.what.startswith("Study today: History, 1848")                     # Physics is 3 days out: pa.py's


def test_the_brief_says_each_thing_once_and_problems_first(migrated):
    """Fix plan item 22: what the top open loops already say leaves the task, mail and school lists;
    a failed backup (its push was held back at night) is the first thing in the brief."""
    import json

    from app.brief import BriefMaker, render_markdown
    from app.db import SettingKV
    from tests.test_pa import PROFILE
    engine = make_engine(migrated.db_url)
    with Session(engine) as s:
        triaged(s, days_ago=1)
        t = Task(title="Hand in the lab report", due=WED + dt.timedelta(days=1), priority=2)
        s.add(t)
        s.add(SettingKV(key="backup_status", value=json.dumps({"last_error": "disk full"})))
        s.commit()
        loops.sync(s, now=WED, tz=TZ, times=TIMES)
        from tests.test_loops import checked
        checked(s)                                       # passed the sanity check: shown
        from app.google.client import GoogleNotConnected

        def not_connected():
            raise GoogleNotConnected("not connected in this test")
        b = BriefMaker(engine=engine, google_read=not_connected, vault_dir=migrated.data_dir / "vault", profile=PROFILE)
        data = b.gather_once(s, WED.date())
        shown = {(lp["kind"], lp["what"][:20]) for lp in data["open_loops"]}
        assert len(shown) == 2
        assert data["needs_reply"] == [] and data["next7"] == []                    # already in "Still open"
        md = render_markdown({**data, "headline": "x", "date": WED.date().isoformat()})
        assert md.index("## Needs attention") < md.index("**x**") and "Backup failed: disk full" in md
