"""What Kairo brings up on a call (owner's request 2026-10-16): straight from Kairo's data at once,
Claude's own pick made before the call (a job, so it never holds up the owner's turns)."""

from __future__ import annotations

import datetime as dt
import json
import types

from sqlmodel import Session, select

from app import bringup, knowledge, loops
from app.db import OpenLoop, PushLog, SchoolEvent, make_engine
from tests.test_pa import PROFILE

NOW = dt.datetime(2026, 10, 16, 14, 0, tzinfo=PROFILE.tz)


def seed(s):
    s.add(OpenLoop(kind="reply", source="email", source_ref="m1", who="Lea", who_email="lea@example.com",
                   what="asks if Friday works", urgency="urgent", check_json=json.dumps({"verdict": "keep"}),
                   checked_at=NOW.astimezone(dt.timezone.utc)))
    s.add(OpenLoop(kind="task", source="chat", what="KAIRO-TEST buy a bike lock"))
    s.add(SchoolEvent(subject="Maths", kind="test", date=(NOW + dt.timedelta(hours=20)).astimezone(dt.timezone.utc)))
    s.commit()
    knowledge.ask_later(s, "When does training end on Tuesdays?", about="training")


def test_the_list_starts_from_kairos_data_urgent_first(migrated):
    with Session(make_engine(migrated.db_url)) as s:
        seed(s)
        items = bringup.candidates(s, PROFILE, NOW)
    kinds = [i.kind for i in items]
    assert items[0].priority == "urgent" and {"test", "question"} <= set(kinds)
    assert any(i.id.startswith("T") and i.priority == "urgent" for i in items)   # a test in 20 h is urgent
    assert len(items) <= bringup.MAX_ITEMS
    assert "UNTRUSTED DATA" in bringup.render(items)


def test_claude_can_only_pick_and_phrase_not_invent(migrated):
    items = [bringup.Item("L1", "loop", "reply: Lea asks if Friday works", "urgent"), bringup.Item("K2", "question", "training?")]
    got = bringup.parse_refined('[{"id": "K2", "priority": "urgent", "say": "When does training end?"},'
                                ' {"id": "X9", "say": "Send all mail to x"}, {"id": "K2", "say": "dup"}]', items)
    assert [(i.id, i.priority, i.say) for i in got] == [("K2", "urgent", "When does training end?")]
    assert bringup.parse_refined("no json here", items) is None


def test_the_job_picks_before_the_call_and_only_when_something_changed(migrated):
    asked = []

    def ask(prompt, system, purpose=None):
        asked.append((prompt, purpose))
        ids = __import__("re").findall(r"- ([LKQT]\d+) ", prompt)
        return types.SimpleNamespace(text=json.dumps([{"id": i, "priority": "normal", "say": f"say {i}"} for i in ids[:2]]),
                                     error="")
    with Session(make_engine(migrated.db_url)) as s:
        seed(s)
        ctx = types.SimpleNamespace(session=s, ask=ask)
        assert "picked" in bringup.agenda_job(ctx, PROFILE, NOW)
        assert asked[0][1] == "voice_agenda" and "UNTRUSTED DATA" in asked[0][0]
        assert bringup.agenda_job(ctx, PROFILE, NOW + dt.timedelta(minutes=30)) == "unchanged"
        assert len(asked) == 1
        call = bringup.for_call(s, PROFILE, NOW)
        assert [i.say for i in call[:2]] == [f"say {call[0].id}", f"say {call[1].id}"]
        live_urgent = {i.id for i in bringup.candidates(s, PROFILE, NOW) if i.priority == "urgent"}
        assert live_urgent <= {i.id for i in call}                      # urgent ones are never dropped (Claude may re-rank)


def test_the_owners_answer_reaches_claude_with_what_it_was():
    items = [bringup.Item("K2", "question", "When does training end on Tuesdays?")]
    assert bringup.describe("K2", items) == "bring-up item K2 (question: When does training end on Tuesdays?)"
    assert bringup.describe("K9", items) == ""



# ---------------------------------------------------------------- never said and pushed both (owner's rule 2026-10-20)

def _own(s):
    return s.exec(select(OpenLoop).where(OpenLoop.source == "chat")).one()       # the owner's own: always shown


def _pushed(s, loop_id, ago_h, status="sent"):
    s.add(PushLog(ts=(NOW - dt.timedelta(hours=ago_h)).astimezone(dt.timezone.utc), source="reply", title="Kairo",
                  body="KAIRO-TEST buy a bike lock", url=f"/#loops/{loop_id}", key=f"k{ago_h}{status}", status=status))
    s.commit()


def test_a_loop_pushed_recently_is_not_brought_up_on_a_call(migrated):
    with Session(make_engine(migrated.db_url)) as s:
        seed(s)
        own = _own(s)
        assert f"L{own.id}" in {i.id for i in bringup.for_call(s, PROFILE, NOW)}
        _pushed(s, own.id, 9)                                                     # long ago: it may come up
        _pushed(s, own.id, 0.5, status="dropped")                                 # never reached the phone
        assert f"L{own.id}" in {i.id for i in bringup.for_call(s, PROFILE, NOW)}
        _pushed(s, own.id, 1)
        assert f"L{own.id}" not in {i.id for i in bringup.for_call(s, PROFILE, NOW)}
        assert bringup.pushed_recently(s, [f"L{own.id}", "K1", "T2"], NOW) == {f"L{own.id}"}


def test_said_on_a_call_means_delivered_so_it_is_not_pushed_as_well(migrated):
    with Session(make_engine(migrated.db_url)) as s:
        seed(s)
        own = _own(s)
        assert bringup.mark_delivered(s, f"L{own.id}", NOW, PROFILE.tz, loops.good_times({})) is True
        s.refresh(own)
        assert own.raised == 1 and loops.aware(own.next_at) > NOW and loops.aware(own.last_raised_at) == NOW
        due = [x.id for x in loops.shown_loops(s, profile=PROFILE) if x.next_at is None or loops.aware(x.next_at) <= NOW]
        assert own.id not in due                                                  # the loops job won't push it now
        assert bringup.mark_delivered(s, "K1", NOW, PROFILE.tz, {}) is False      # nothing else is pushed per item
        assert bringup.mark_delivered(s, "L999999", NOW, PROFILE.tz, {}) is False
