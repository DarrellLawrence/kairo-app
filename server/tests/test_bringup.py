"""What Kairo brings up on a call (owner's request 2026-10-16): straight from Kairo's data at once,
Claude's own pick made before the call (a job, so it never holds up the owner's turns)."""

from __future__ import annotations

import datetime as dt
import json
import types

from sqlmodel import Session

from app import bringup, knowledge
from app.db import OpenLoop, SchoolEvent, make_engine
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
