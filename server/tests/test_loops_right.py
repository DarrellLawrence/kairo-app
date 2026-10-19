"""Open loops done right (owner's request 2026-10-15). Written before the code.

The real case: a promise loop said "You promised <the owner> yesterday: attend the project meeting
Thursday 17:30", drafted "Hi <owner>, I'll be at the meeting", claimed "calendar is empty for the
next 7 days" (the owner has school Thu/Fri), was marked Urgent, and Send said "Kairo doesn't know
which of the people on that mail this is for. Send it yourself and tap Done."

Fixed at the causes:
- identity: no loop, draft or card is ever addressed to the owner; a promise is to the others;
- the right action per loop: a promise to attend = a calendar event + reminder, no message;
- calendar truth: availability only from a calendar read that worked (7 days, every calendar);
- Send with several people: a picker (name + full address), then the normal card;
- urgency: Urgent only when something bad happens if the owner doesn't act today;
- a Sonnet sanity pass before anything is shown: fix it or drop it, drops logged.
Made-up people only (the repo never holds the owner's data).
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlmodel import Session, select

from app import identity, loops
from app.db import OpenLoop, TriagedEmail
from app.executor import Executor
from app.gate import load_gate
from app.google.client import GoogleAPI
from app.profile import Profile
from app.settings import REPO_ROOT
from tests.fakes.fake_google import FakeGoogle, connect
from tests.test_loops import Ctx, Pushes, TIMES

OWNER = Profile(name="Sam", default_language="en", languages=["en", "de"], timezone="Europe/Zurich", context="",
                gmail="sam.rivera@example.com", quiet_hours={"start": "22:00", "end": "07:00"})
TZ = OWNER.tz
WED = dt.datetime(2026, 10, 14, 10, 0, tzinfo=TZ)     # Wednesday 10:00
THU_1730 = dt.datetime(2026, 10, 15, 17, 30, tzinfo=TZ)


@pytest.fixture
def env(migrated):
    from app.db import make_engine
    fake = FakeGoogle(owner=OWNER.gmail)
    fake.send_as = [{"sendAsEmail": OWNER.gmail, "displayName": "Sam Rivera", "isPrimary": True}]
    secrets = migrated.data_dir / "secrets"
    connect(secrets, fake)
    engine = make_engine(migrated.db_url)
    read = lambda: GoogleAPI(secrets, "read", transport=fake.transport())  # noqa: E731
    gate = load_gate(REPO_ROOT / "config")
    executor = Executor(secrets_dir=secrets, transport=fake.transport(), owner_email=OWNER.gmail)
    told = []
    return dict(fake=fake, engine=engine, read=read, gate=gate, executor=executor, told=told, migrated=migrated)


def finder(env):
    from app import own_calendar
    return loops.PromiseFinder(
        profile=OWNER, schedule={}, google_read=env["read"],
        hold=lambda args, reason: own_calendar.hold(env["engine"], env["gate"], env["executor"], args, reason=reason),
        tell=env["told"].append)


def sent_mail(fake, mid="s1", to="Lea Muster <lea@example.com>, Tom Weber <tom@example.com>", body="", cc=None,
              sender="Sam Rivera <sam.rivera@example.com>"):
    fake.add_message(mid, sender=sender, subject="Project meeting", to=to, cc=cc, body=body, thread="t-" + mid)


ATTEND = {"message_id": "s1", "to_name": "Lea Muster", "to_email": "lea@example.com", "type": "attend",
          "what": "attend the project meeting", "when": "2026-10-15T17:30", "minutes": 60, "by": None}


# ---------------------------------------------------------------- 1. a promise to attend

def test_a_promise_to_attend_becomes_a_calendar_event_and_no_message(env):
    """Acceptance 36: promise to attend -> a calendar event (own calendar, reminder), no message."""
    sent_mail(env["fake"], body="Sure, I'll be at the project meeting Thursday 17:30.")
    with Session(env["engine"]) as s:
        out = json.loads(finder(env).run(Ctx(s, {"new": [ATTEND], "fulfilled": []}), now=WED))
        assert out["new"] == 1 and out["calendar"] == 1
        loop = s.exec(select(OpenLoop)).one()
        assert loop.action == "calendar" and loop.status == "done" and loop.draft_json is None
        assert loop.close_reason == "calendar"
    ev = next(iter(env["fake"].events.values()))
    assert ev["start"]["dateTime"].startswith("2026-10-15T17:30") and ev["end"]["dateTime"].startswith("2026-10-15T18:30")
    assert ev["reminders"]["overrides"] and not ev.get("attendees") and ev["_sendUpdates"] == "none"
    assert "project meeting" in ev["summary"].lower()
    assert env["fake"].sent == [] and env["fake"].drafts == []                 # nobody gets a message
    assert len(env["told"]) == 1 and "calendar" in env["told"][0].lower() and "17:30" in env["told"][0]


def test_an_attend_promise_in_the_past_or_without_a_time_is_a_reminder_not_an_event(env):
    sent_mail(env["fake"], body="I'll come to the meeting.")
    vague = {**ATTEND, "when": None}
    with Session(env["engine"]) as s:
        finder(env).run(Ctx(s, {"new": [vague], "fulfilled": []}), now=WED)
        loop = s.exec(select(OpenLoop)).one()
        assert loop.action == "remind" and loop.status == "open" and loop.draft_json is None
    assert env["fake"].events == {}


def test_a_promise_to_send_something_drafts_a_message_to_the_other_person(env):
    sent_mail(env["fake"], body="I'll send you the slides tomorrow.")
    p = {**ATTEND, "type": "send", "what": "send the slides", "when": None, "by": "2026-10-15"}
    with Session(env["engine"]) as s:
        finder(env).run(Ctx(s, {"new": [p], "fulfilled": []}), now=WED)
        loop = s.exec(select(OpenLoop)).one()
        assert loop.action == "message" and loop.status == "open"
        assert (loop.who, loop.who_email, loop.mail_ref) == ("Lea Muster", "lea@example.com", "s1")
        assert json.loads(loop.people_json) == [{"name": "Lea Muster", "email": "lea@example.com"},
                                                {"name": "Tom Weber", "email": "tom@example.com"}]


# ---------------------------------------------------------------- 2. never addressed to the owner

def test_no_promise_is_ever_to_the_owner(env):
    """Acceptance 37: no loop ever addressed to the owner."""
    sent_mail(env["fake"], body="I'll be at the project meeting Thursday 17:30.")
    to_me = {**ATTEND, "type": "send", "to_name": "Sam Rivera", "to_email": "sam.rivera@example.com", "what": "send it"}
    by_name = {**ATTEND, "type": "send", "to_name": "Sam", "to_email": "", "what": "send the plan"}
    with Session(env["engine"]) as s:
        ctx = Ctx(s, {"new": [to_me, by_name], "fulfilled": []})
        finder(env).run(ctx, now=WED)
        prompt = ctx.prompts[0][1]
        assert "Sam Rivera" in prompt and "sam.rivera@example.com" in prompt   # the model is told who the owner is
        rows = s.exec(select(OpenLoop)).all()
    # the claimed owner is replaced by the real other people on the mail, never kept
    assert all(r.who_email != "sam.rivera@example.com" and (r.who or "").lower() not in ("sam", "sam rivera") for r in rows)


def test_mail_to_yourself_is_never_read_for_promises(env):
    sent_mail(env["fake"], mid="self", to="sam.rivera@example.com", body="Kairo recap: I'll call the dentist.")
    with Session(env["engine"]) as s:
        ctx = Ctx(s, {"new": [], "fulfilled": []})
        with pytest.raises(loops.Skipped):
            finder(env).run(ctx, now=WED)
        assert ctx.prompts == []


def test_a_mail_from_the_owners_other_address_is_not_a_reply_loop(env):
    with Session(env["engine"]) as s:
        identity.load(s, OWNER, google_read=env["read"])
        s.add(TriagedEmail(message_id="m9", thread_id="t9", sender="Sam Rivera <sam.rivera@example.com>", subject="note",
                           summary="note to self", category="reply", received_at=WED.astimezone(dt.timezone.utc),
                           triaged_at=dt.datetime.now(dt.timezone.utc), personal_reply=True))
        s.commit()
        owner = identity.load(s, OWNER)
        assert loops.sync(s, now=WED, tz=TZ, times=TIMES, owner=owner)["new"] == 0


# ---------------------------------------------------------------- 3. calendar truth

def test_the_calendar_read_covers_seven_days_and_every_calendar(env):
    from app import calendar_read
    env["fake"].add_event("e1", summary="School", start="2026-10-15T08:00:00+02:00", end="2026-10-15T16:30:00+02:00")
    env["fake"].add_event("e2", summary="Training", start="2026-10-16T18:00:00+02:00", end="2026-10-16T19:30:00+02:00",
                          calendar="sport@group.calendar.example")
    env["fake"].add_event("e3", summary="Far away", start="2026-10-30T09:00:00+01:00")
    with Session(env["engine"]) as s:
        r = calendar_read.read_days(env["read"], TZ, WED.date(), days=7, session=s)
        assert r.ok and [e["summary"] for e in r.events] == ["School", "Training"]
        text = r.text(TZ)
        assert "Thu 15 Oct 08:00–16:30 School" in text and "Fri 16 Oct 18:00–19:30 Training" in text
        assert calendar_read.last(s)["ok"] is True and calendar_read.last(s)["calendars"] == 2


def test_a_failed_calendar_read_is_never_an_empty_calendar(env):
    """The bug: 'calendar is empty for the next 7 days' while the owner has school Thu/Fri."""
    from app import calendar_read
    env["fake"].add_event("e1", summary="School", start="2026-10-15T08:00:00+02:00")
    env["fake"].calendar_down = True
    with Session(env["engine"]) as s:
        r = calendar_read.read_days(env["read"], TZ, WED.date(), days=7, session=s)
        assert not r.ok and r.events == []
        assert "could not be read" in r.text(TZ).lower() and "nothing about" in r.text(TZ).lower()
        assert calendar_read.last(s)["ok"] is False and calendar_read.last(s)["error"]


def test_the_draft_says_nothing_about_free_times_without_a_calendar(env):
    from app import calendar_read
    env["fake"].calendar_down = True
    env["fake"].add_message("m1", sender="Lea Muster <lea@example.com>", subject="Meeting?",
                            body="Does Thursday 17:30 work?", thread="t1")
    with Session(env["engine"]) as s:
        loop = OpenLoop(kind="reply", source="email", source_ref="m1", what="if Thursday 17:30 works", who="Lea Muster",
                        who_email="lea@example.com", since=WED, action="reply")
        s.add(loop)
        s.commit()
        eng = loops.LoopEngine(engine=env["engine"], profile=OWNER, schedule={}, google_read=env["read"],
                               calendar=lambda start: calendar_read.read_days(env["read"], TZ, start, days=7, session=s),
                               pusher=Pushes(), vault_dir=env["migrated"].data_dir / "vault", config_dir=REPO_ROOT / "config")
        ctx = Ctx(s, {"text": "Hi Lea, let me check.", "note": "x"}, {"text": "Hi Lea, let me check."})
        eng.draft(ctx, loop)
        prompt = next(p for purpose, p in ctx.prompts if purpose == "draft")
    assert "(nothing)" not in prompt and "could not be read" in prompt.lower()


def test_the_timetable_check_says_what_is_wrong(env):
    """Owner: 'my timetable must be in the calendar as weekly FIXED blocks'. The check tells which part broke."""
    from app import calendar_read, timetable
    from app.db import CalendarTag, SettingKV
    with Session(env["engine"]) as s:
        assert calendar_read.timetable_status(s, env["read"])["state"] == "not imported"
        env["fake"].events["ev7"] = {"id": "ev7", "summary": "[Kairo] School", "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=TH"],
                                     "start": {"dateTime": "2026-10-15T08:00:00+02:00"}}
        s.add(SettingKV(key=timetable.KEY, value=json.dumps({"until": "2027-01-31", "events": [
            {"day": "TH", "start": "08:00", "end": "16:30", "event_id": "ev7"},
            {"day": "FR", "start": "08:00", "end": "12:00", "event_id": "ev8"}]})))
        s.add(CalendarTag(event_id="ev7", type="FIXED"))
        s.commit()
        st = calendar_read.timetable_status(s, env["read"])
        assert st["state"] == "events missing" and st["missing"] == ["FR 08:00–12:00"] and st["present"] == 1
        assert s.get(CalendarTag, "ev7").type == "FIXED"
        env["fake"].calendar_down = True
        assert calendar_read.timetable_status(s, env["read"])["state"] == "calendar unreadable"


# ---------------------------------------------------------------- 4. Send with several people: the picker

@pytest.fixture
def client(env):
    from fastapi.testclient import TestClient
    from app.main import create_app
    app = create_app(env["migrated"])
    app.state.executor = Executor(secrets_dir=env["migrated"].data_dir / "secrets", transport=env["fake"].transport(),
                                  owner_email=OWNER.gmail)
    with TestClient(app) as c:
        yield c


def promise_loop(env, **kw) -> int:
    sent_mail(env["fake"], body="I'll send you both the slides.")
    base = dict(kind="promise", source="promise", source_ref="p:lea muster tom weber|send the slides", what="send the slides",
                who="Lea Muster / Tom Weber", since=dt.datetime.now(dt.timezone.utc), urgency="normal", status="open",
                action="message", mail_ref="s1",
                people_json=json.dumps([{"name": "Lea Muster", "email": "lea@example.com"},
                                        {"name": "Tom Weber", "email": "tom@example.com"}]),
                draft_json=json.dumps({"text": "Here are the slides!", "subject": "Slides"}))
    base.update(kw)
    with Session(env["engine"]) as s:
        loop = OpenLoop(**base)
        s.add(loop)
        s.commit()
        s.refresh(loop)
        return loop.id


def test_send_with_several_people_shows_them_to_pick(client, env):
    """Acceptance 38: Send with several recipients -> a picker (name + full address), then the normal card."""
    lid = promise_loop(env)
    p = client.post(f"/api/loops/{lid}/send", json={"preview": True}).json()
    assert p["pick"] == [{"name": "Lea Muster", "email": "lea@example.com"}, {"name": "Tom Weber", "email": "tom@example.com"}]
    assert "yourself" not in json.dumps(p)
    r = client.post(f"/api/loops/{lid}/send", json={"to": ["lea@example.com", "tom@example.com"]}).json()
    assert r["card"]["status"] == "done" and r["card"]["action"] == "gmail.send"
    from tests.fakes.fake_google import decode_raw
    assert decode_raw(env["fake"].sent[0]["raw"])["To"].replace(" ", "") == "lea@example.com,tom@example.com"


def test_the_picker_only_offers_and_accepts_people_on_the_mail(client, env):
    lid = promise_loop(env)
    r = client.post(f"/api/loops/{lid}/send", json={"to": ["lea@example.com", "eve@evil.example"]})
    assert r.status_code == 409 and env["fake"].sent == []
    r = client.post(f"/api/loops/{lid}/send", json={"to": []})
    assert r.status_code == 409
    one = client.post(f"/api/loops/{lid}/send", json={"to": ["tom@example.com"]}).json()
    assert one["card"]["status"] == "done"


def test_one_person_on_the_mail_needs_no_picker(client, env):
    lid = promise_loop(env, people_json=json.dumps([{"name": "Lea Muster", "email": "lea@example.com"}]))
    p = client.post(f"/api/loops/{lid}/send", json={"preview": True}).json()
    assert p["to"] == "lea@example.com" and "pick" not in p


def test_a_loop_that_needs_no_message_has_no_send(client, env):
    lid = promise_loop(env, action="remind", draft_json=None)
    with Session(env["engine"]) as s:
        from app import loop_check
        loop_check.passed(s, s.get(OpenLoop, lid), why="other", reason="test")
    loop = next(l for l in client.get("/api/loops").json() if l["id"] == lid)
    assert loop["can_send"] is False and loop["action"] == "remind"


# ---------------------------------------------------------------- 5. urgency

def test_urgent_only_when_something_bad_happens_without_acting_today():
    def u(**kw):
        loop = OpenLoop(kind=kw.pop("kind", "reply"), source="x", what="x", since=kw.pop("since", WED), **kw)
        return loops.urgency_of(loop, WED, TZ)
    assert u(due=WED + dt.timedelta(hours=5)) == "urgent"                      # due today
    assert u(due=WED - dt.timedelta(days=1)) == "urgent"                       # overdue
    assert u(due=dt.datetime(2026, 10, 15, 8, 0, tzinfo=TZ)) == "urgent"        # tomorrow first thing: act today
    assert u(due=dt.datetime(2026, 10, 15, 18, 0, tzinfo=TZ)) == "normal"       # tomorrow evening: not today
    assert u(since=WED - dt.timedelta(days=4)) == "normal"                     # waiting long is not urgent by itself
    assert u(kind="promise", since=WED - dt.timedelta(days=1)) == "normal"     # the screenshot case: not urgent
    assert u(kind="deadline", due=WED + dt.timedelta(days=4)) == "normal"
    assert u(kind="task") == "low"


# ---------------------------------------------------------------- 6. the sanity check (Sonnet) before anything is shown

class CheckCtx:
    """The model for the check: answers {loop id: verdict} from `verdicts`, or fails."""

    def __init__(self, s, verdicts=None, down=False):
        self.session, self.verdicts, self.down, self.prompts = s, verdicts or {}, down, []

    def ask(self, prompt, system, images=None, purpose=None):
        from app.chat import AskResult
        self.prompts.append((purpose, prompt))
        if self.down:
            return AskResult("", status="error", error="model unavailable")
        return AskResult(json.dumps(self.verdicts))


def checker(env):
    from app import calendar_read, loop_check, own_calendar
    return loop_check.LoopChecker(
        profile=OWNER, google_read=env["read"],
        calendar=lambda start: calendar_read.read_days(env["read"], TZ, start, days=7),
        hold=lambda args, reason: own_calendar.hold(env["engine"], env["gate"], env["executor"], args, reason=reason),
        tell=env["told"].append)


def screenshot_loop(s, **kw):
    """The real case from the owner's screenshot, with made-up names."""
    base = dict(kind="promise", source="promise", source_ref="p:sam rivera|attend the project meeting thursday 17 30",
                what="attend the project meeting Thursday 17:30", who="Sam Rivera", since=WED - dt.timedelta(days=1),
                urgency="urgent", draft_json=json.dumps({"text": "Hi Sam, I'll be at the meeting.",
                                                         "note": "Your calendar is empty for the next 7 days."}))
    base.update(kw)
    loop = OpenLoop(**base)
    s.add(loop)
    s.commit()
    s.refresh(loop)
    return loop


def test_the_screenshot_loop_becomes_a_calendar_event(env):
    with Session(env["engine"]) as s:
        loop = screenshot_loop(s)
        ctx = CheckCtx(s, {str(loop.id): {"verdict": "fix", "why": "action",
                                          "reason": "a promise to attend belongs in the calendar, not a mail to yourself",
                                          "fix": {"action": "calendar", "when": "2026-10-15T17:30"}}})
        out = checker(env).run(ctx, now=WED)
        s.refresh(loop)
        assert out["fixed"] == 1 and loop.status == "done" and loop.close_reason == "calendar"
        assert ctx.prompts[0][0] == "loop_check" and "Sam Rivera" in ctx.prompts[0][1]   # the check knows the owner
    assert len(env["fake"].events) == 1 and env["fake"].sent == []
    assert any("calendar" in t.lower() for t in env["told"])


def test_a_loop_addressed_to_the_owner_is_never_shown_even_if_the_model_says_ok(env):
    with Session(env["engine"]) as s:
        loop = screenshot_loop(s, kind="reply", source="email", source_ref="m1", who_email="sam.rivera@example.com")
        checker(env).run(CheckCtx(s, {str(loop.id): {"verdict": "ok", "reason": "fine"}}), now=WED)
        s.refresh(loop)
        assert loop.status != "open" and "addressed to you" in loop.close_reason


def test_duplicates_and_noise_are_dropped_and_logged(env):
    with Session(env["engine"]) as s:
        a = OpenLoop(kind="waiting", source="chat", source_ref="c1", what="Lea sends the room booking", who="Lea Muster",
                     since=WED, urgency="normal")
        b = OpenLoop(kind="waiting", source="email", source_ref="m2", what="room booking from Lea", who="Lea Muster",
                     since=WED, urgency="normal")
        s.add(a)
        s.add(b)
        s.commit()
        out = checker(env).run(CheckCtx(s, {str(b.id): {"verdict": "drop", "why": "duplicate", "reason": "same as the other",
                                                         "fix": {"duplicate_of": a.id}},
                                            str(a.id): {"verdict": "ok", "reason": "a real open thing"}}), now=WED)
        s.refresh(a)
        s.refresh(b)
        assert out["dropped"] == 1 and a.status == "open" and b.status != "open"
        assert b.close_reason == f"check: duplicate of #{a.id}"
        assert json.loads(b.check_json)["why"] == "duplicate"


def test_a_fix_can_never_set_an_address_or_make_the_owner_the_person(env):
    with Session(env["engine"]) as s:
        loop = OpenLoop(kind="promise", source="promise", source_ref="p:x|send the slides", what="send the slides",
                        who="Lea Muster", who_email="lea@example.com", since=WED, urgency="normal", action="message",
                        people_json=json.dumps([{"name": "Lea Muster", "email": "lea@example.com"},
                                                {"name": "Tom Weber", "email": "tom@example.com"}]))
        s.add(loop)
        s.commit()
        checker(env).run(CheckCtx(s, {str(loop.id): {"verdict": "fix", "why": "person", "reason": "x",
                                                     "fix": {"who": "Sam Rivera", "who_email": "evil@x.example"}}}), now=WED)
        s.refresh(loop)
        assert (loop.who, loop.who_email) == ("Lea Muster", "lea@example.com")
        loop.check_json = None                                                     # check it again
        s.add(loop)
        s.commit()
        checker(env).run(CheckCtx(s, {str(loop.id): {"verdict": "fix", "why": "person", "reason": "it was Tom who asked",
                                                     "fix": {"who": "Tom Weber"}}}), now=WED)
        s.refresh(loop)
        assert (loop.who, loop.who_email) == ("Tom Weber", "tom@example.com")      # only someone on the mail


def test_unchecked_loops_are_not_shown_and_a_changed_loop_is_checked_again(env):
    with Session(env["engine"]) as s:
        loop = OpenLoop(kind="waiting", source="email", source_ref="m3", what="Lea's answer on the offer", who="Lea Muster",
                        since=WED, urgency="normal")
        s.add(loop)
        s.commit()
        owner = identity.load(s, OWNER)
        assert loops.shown_loops(s, owner) == []
        ck = checker(env)
        ck.run(CheckCtx(s, {str(loop.id): {"verdict": "ok", "reason": "real"}}), now=WED)
        assert [l.id for l in loops.shown_loops(s, owner)] == [loop.id]
        loop.what = "Lea's answer on the new offer"
        s.add(loop)
        s.commit()
        assert loops.shown_loops(s, owner) == []                                 # changed: hidden until checked again
        ctx = CheckCtx(s, {str(loop.id): {"verdict": "ok", "reason": "real"}})
        assert ck.run(ctx, now=WED)["checked"] == 1


def test_model_down_hides_loops_except_what_cannot_wait(env):
    with Session(env["engine"]) as s:
        later = OpenLoop(kind="waiting", source="email", source_ref="m4", what="x", who="Lea Muster", since=WED, urgency="normal")
        today = OpenLoop(kind="deadline", source="school", source_ref="7", what="hand in the form", since=WED,
                         due=WED + dt.timedelta(hours=4), urgency="urgent")
        s.add(later)
        s.add(today)
        s.commit()
        checker(env).run(CheckCtx(s, down=True), now=WED)
        shown = loops.shown_loops(s, identity.load(s, OWNER))
        assert [l.id for l in shown] == [today.id]
        assert json.loads(s.get(OpenLoop, today.id).check_json)["error"]


def test_the_first_check_tells_the_owner_how_many_were_fixed_and_dropped(env):
    with Session(env["engine"]) as s:
        a = screenshot_loop(s)
        b = OpenLoop(kind="waiting", source="email", source_ref="m5", what="newsletter", who="Shop", since=WED, urgency="normal")
        c = OpenLoop(kind="waiting", source="email", source_ref="m6", what="Lea's answer", who="Lea Muster", since=WED,
                     urgency="normal")
        for x in (b, c):
            s.add(x)
        s.commit()
        verdicts = {str(a.id): {"verdict": "fix", "why": "action", "reason": "calendar",
                                "fix": {"action": "calendar", "when": "2026-10-15T17:30"}},
                    str(b.id): {"verdict": "drop", "why": "noise", "reason": "a shop mailing"},
                    str(c.id): {"verdict": "ok", "reason": "real"}}
        ck = checker(env)
        ck.run(CheckCtx(s, verdicts), now=WED)
        assert env["told"][-1] == "Checked your 3 open loops: 1 fixed, 1 dropped, 1 fine. Details: More → System → Run diagnosis."
        n = len(env["told"])
        d = OpenLoop(kind="waiting", source="email", source_ref="m7", what="y", who="Tom Weber", since=WED, urgency="normal")
        s.add(d)
        s.commit()
        ck.run(CheckCtx(s, {str(d.id): {"verdict": "ok", "reason": "real"}}), now=WED)
        assert len(env["told"]) == n                                             # only the first time


def test_missing_school_blocks_are_put_back_once_a_day(env):
    """'My timetable must be in the calendar as weekly FIXED blocks': a block that went missing comes back
    (same day, times, weekly until the semester ends), tagged FIXED, with one line to the owner."""
    from app import calendar_read, own_calendar, timetable
    from app.db import CalendarTag, SettingKV
    env["fake"].events["ev7"] = {"id": "ev7", "summary": "[Kairo] School", "start": {"dateTime": "2026-10-15T08:00:00+02:00"}}
    with Session(env["engine"]) as s:
        s.add(SettingKV(key=timetable.KEY, value=json.dumps({"until": "2027-01-31", "events": [
            {"day": "TH", "start": "08:00", "end": "16:30", "event_id": "ev7"},
            {"day": "FR", "start": "08:00", "end": "12:00", "event_id": "gone"}]})))
        s.commit()
        hold = lambda args, reason: own_calendar.hold(env["engine"], env["gate"], env["executor"], args, reason=reason)  # noqa: E731
        out = calendar_read.timetable_daily(s, env["read"], TZ, now=WED, hold=hold, tell=env["told"].append)
        assert out["state"] == "repaired" and out["put_back"] == ["FR 08:00–12:00"]
        new = [e for k, e in env["fake"].events.items() if k != "ev7"][0]
        assert new["recurrence"][0].startswith("RRULE:FREQ=WEEKLY;BYDAY=FR;UNTIL=2027") and new["start"]["dateTime"].startswith("2026-10-16T08:00")
        assert s.get(CalendarTag, new["id"]).type == "FIXED" and s.get(CalendarTag, "ev7").type == "FIXED"
        assert "FR 08:00–12:00" not in [e["event_id"] for e in json.loads(s.get(SettingKV, timetable.KEY).value)["events"]]
        assert len(env["told"]) == 1 and "school" in env["told"][0].lower()
        assert calendar_read.timetable_daily(s, env["read"], TZ, now=WED + dt.timedelta(hours=2), hold=hold,
                                             tell=env["told"].append) is None            # once a day


def test_the_owners_own_tasks_and_coach_need_no_check(env):
    with Session(env["engine"]) as s:
        t = OpenLoop(kind="task", source="task", source_ref="9", what="tidy desk", since=WED, urgency="low")
        s.add(t)
        s.commit()
        ctx = CheckCtx(s, {})
        assert checker(env).run(ctx, now=WED)["checked"] == 0 and ctx.prompts == []
        assert [l.id for l in loops.shown_loops(s, identity.load(s, OWNER))] == [t.id]


def test_a_task_triage_made_from_a_mail_is_checked_like_the_mail(env):
    """2026-10-19: a shop's feedback mail became a task loop and skipped the check as if the owner had made it."""
    from app.db import Task
    with Session(env["engine"]) as s:
        task = Task(title="Is everything fine with your Shopco order? (from Shopco)", source="email", priority=2)
        s.add(task)
        s.commit()
        s.refresh(task)
        t = OpenLoop(kind="task", source="task", source_ref=str(task.id), what=task.title, since=WED, urgency="low")
        s.add(t)
        s.commit()
        assert loops.shown_loops(s, identity.load(s, OWNER)) == []                # unchecked: not shown
        ctx = CheckCtx(s, {str(t.id): {"verdict": "drop", "why": "noise", "reason": "a shop's feedback mail"}})
        assert checker(env).run(ctx, now=WED)["dropped"] == 1
        s.refresh(t)
        assert t.status != "open" and t.close_reason == "check: a shop's feedback mail"
