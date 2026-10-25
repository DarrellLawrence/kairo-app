"""G10: the timer sentences a rule answers (app/fastpath.py + timers.py): setting, asking, cancelling. Real database, real Timers,
an injected clock. A timer is an internal action that only touches the owner: no approval card, no gate action, no model."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, select

from app import fastpath, flags
from app import timers as timers_mod
from app.db import Timer
from app.timers import Timers
from tests.test_pa import TZ
from tests.test_timers import Bell, Clock, db_template, engine  # noqa: F401  (fixtures: one migrated database per file)

T0 = dt.datetime(2026, 10, 14, 10, 0, tzinfo=TZ)


@pytest.fixture
def rig(engine):
    clock, bell = Clock(T0), Bell()
    timers = Timers(engine, bell, tz=TZ, now=clock)
    fp = fastpath.FastPaths(tz=TZ, now=clock, engine=engine, timers=timers)
    r = type("Rig", (), {})()
    r.engine, r.clock, r.bell, r.timers, r.fp = engine, clock, bell, timers, fp
    r.rules = fp.rules(time_date=True, timers=True)
    r.say = lambda text, **kw: (lambda a: a.text if a else None)(r.rules.resolve(text, **kw))
    r.rows = lambda: list(Session(engine).exec(select(Timer).order_by(Timer.id)).all())
    return r


def test_setting_a_timer_makes_one_row_and_says_how_long(rig):
    assert rig.say("Set a timer for ten minutes") == "Okay, 10 minutes."
    [row] = rig.rows()
    assert (row.seconds, row.label, row.kind, row.lang, row.status) == (600, None, "timer", "en", "active")
    assert row.fire_at.replace(tzinfo=dt.timezone.utc) == (T0 + dt.timedelta(minutes=10)).astimezone(dt.timezone.utc)


def test_a_german_timer_is_set_in_german_and_will_ring_in_german(rig):
    assert rig.say("Stell einen Timer auf eine halbe Stunde") == "Okay, 30 Minuten."
    assert rig.say("Stell einen Timer auf 90 Sekunden für die Eier") == "Okay, 1 Minute 30 Sekunden: die Eier."
    assert [(r.seconds, r.label, r.lang) for r in rig.rows()] == [(1800, None, "de"), (90, "die Eier", "de")]
    rig.clock.advance(seconds=95)
    assert rig.timers.fire_due() == 1 and rig.bell.calls[0]["body"] == "Dein Timer für 1 Minute 30 Sekunden ist abgelaufen: die Eier (gestellt um 10:00)."


def test_a_label_and_a_reminder_say_what_they_will_do(rig):
    assert rig.say("set a timer for 10 minutes for the pasta") == "Okay, 10 minutes: the pasta."
    assert rig.say("remind me in 20 minutes to call Lea") == "Okay, I'll remind you in 20 minutes: call Lea."
    assert rig.say("remind me in an hour") == "Okay, I'll remind you in 1 hour."
    assert rig.say("Erinner mich in 20 Minuten an die Wäsche") == "Okay, ich erinnere dich in 20 Minuten: an die Wäsche."
    assert [(r.kind, r.label) for r in rig.rows()] == [("timer", "the pasta"), ("reminder", "call Lea"), ("reminder", None), ("reminder", "an die Wäsche")]


def test_asking_whether_a_rule_would_answer_sets_nothing(rig):
    assert rig.rules.would_answer("set a timer for ten minutes") is True
    assert rig.rules.would_answer("cancel the timer") is True
    assert rig.rows() == []
    rig.say("set a timer for ten minutes")
    assert rig.rules.would_answer("cancel the timer") is True and [r.status for r in rig.rows()] == ["active"]


def test_a_length_that_cannot_be_set_is_told_not_guessed(rig):
    assert rig.say("set a timer for 30 hours") == "A timer can be from a second to a day."
    assert rig.say("stell einen Timer auf 30 Stunden") == "Ein Timer geht von einer Sekunde bis zu einem Tag."
    assert rig.rows() == []
    for _ in range(timers_mod.MAX_ACTIVE):
        rig.timers.add(600)
    assert rig.say("set a timer for 5 minutes") == f"There are already {timers_mod.MAX_ACTIVE} timers running."
    assert rig.say("stell einen Timer auf 5 Minuten") == f"Es laufen schon {timers_mod.MAX_ACTIVE} Timer."
    assert len(rig.rows()) == timers_mod.MAX_ACTIVE


def test_how_long_is_left(rig):
    assert rig.say("how much time is left on my timer") == "I don't have a timer running."
    assert rig.say("wie lange läuft der Timer noch") == "Ich habe keinen Timer laufen."
    rig.say("set a timer for ten minutes")
    rig.clock.advance(minutes=4)
    assert rig.say("how much time is left on my timer") == "About 6 minutes left."
    assert rig.say("wie lange läuft der Timer noch") == "Noch etwa 6 Minuten."
    rig.clock.advance(minutes=5, seconds=18)
    assert rig.say("how much time is left on the timer") == "About 40 seconds left."
    rig.say("set a timer for an hour for the laundry")
    got = rig.say("how much time is left on the timer")
    assert got == ("You have 2 timers. The timer for 10 minutes: about 40 seconds left. The laundry: about 1 hour left.")
    assert rig.say("how long is left on the laundry timer") == "The laundry: about 1 hour left."
    assert rig.say("how long is left on the pizza timer") == "I don't have a timer called pizza."


def test_which_timers_there_are(rig):
    assert rig.say("which timers do I have") == "I don't have a timer running."
    rig.say("set a timer for ten minutes for the pasta")
    assert rig.say("do I have a timer running") == "The pasta: about 10 minutes left."
    rig.say("set a timer for 5 minutes")
    rig.say("set a timer for 20 minutes")
    rig.say("set a timer for 30 minutes")
    got = rig.say("which timers do I have")
    assert got == ("You have 4 timers. The timer for 5 minutes: about 5 minutes left. The pasta: about 10 minutes left. "
                   "The timer for 20 minutes: about 20 minutes left. And 1 more.")
    assert rig.say("welche Timer habe ich").startswith("Du hast 4 Timer. Der Timer für 5 Minuten: noch etwa 5 Minuten.")


def test_cancelling_the_only_timer_needs_no_name(rig):
    assert rig.say("cancel the timer") == "I don't have a timer running."
    rig.say("set a timer for ten minutes")
    assert rig.say("cancel the timer") == "Okay, timer cancelled."
    assert [r.status for r in rig.rows()] == ["cancelled"]
    rig.clock.advance(minutes=20)
    assert rig.timers.fire_due() == 0 and rig.bell.calls == []                       # it never rings
    rig.say("Stell einen Timer auf 5 Minuten")
    assert rig.say("Timer abbrechen") == "Okay, Timer abgebrochen."


def test_with_several_timers_cancel_asks_which_and_cancels_none(rig):
    rig.say("set a timer for ten minutes")
    rig.say("set a timer for 5 minutes for the tea")
    got = rig.say("cancel the timer")
    assert got.startswith("You have 2 timers.") and got.endswith("Say cancel all timers, or tell me which one by its name or length.")
    assert [r.status for r in rig.rows()] == ["active", "active"]
    assert rig.say("Timer stoppen").endswith("Sag alle Timer abbrechen, oder welchen, mit Namen oder Länge.")
    assert [r.status for r in rig.rows()] == ["active", "active"]


def test_cancel_one_by_its_length_or_its_name_or_all(rig):
    for t in ("set a timer for ten minutes", "set a timer for 5 minutes for the tea", "set a timer for 20 minutes for the oven door"):
        rig.say(t)
    assert rig.say("cancel the ten minute timer") == "Okay, timer cancelled."
    assert rig.say("cancel the tea timer") == "Okay, timer cancelled."
    assert [r.status for r in rig.rows()] == ["cancelled", "cancelled", "active"]
    assert rig.say("cancel the pizza timer") == "I don't have a timer called pizza."
    assert rig.say("cancel the 7 minute timer") == "I don't have a timer for 7 minutes."
    assert rig.say("cancel all timers") == "Okay, timer cancelled."                  # one left: said for one
    rig.say("set a timer for 1 minute")
    rig.say("set a timer for 2 minutes")
    assert rig.say("cancel all timers") == "Okay, all 2 timers cancelled."
    rig.say("stell einen Timer auf 3 Minuten")
    rig.say("stell einen Timer auf 4 Minuten")
    assert rig.say("alle Timer abbrechen") == "Okay, alle 2 Timer abgebrochen."
    assert rig.say("den Nudel Timer abbrechen") == "Ich habe keinen Timer laufen."
    rig.say("stell einen Timer auf 3 Minuten für die Eier")
    assert rig.say("den Nudel Timer abbrechen") == "Ich habe keinen Timer namens nudel."
    assert rig.say("den Eier Timer abbrechen") == "Okay, Timer abgebrochen."
    assert {r.status for r in rig.rows()} == {"cancelled"}


def test_two_timers_of_the_same_length_are_not_cancelled_blindly(rig):
    rig.say("set a timer for ten minutes")
    rig.say("set a timer for ten minutes")
    got = rig.say("cancel the ten minute timer")
    assert got.startswith("You have 2 timers.") and [r.status for r in rig.rows()] == ["active", "active"]


# ---------------------------------------------------------------- the switch

def test_g10_timers_is_on_by_default_and_off_means_no_timer_sentence_is_answered(rig):
    assert flags.FLAGS["g10.timers"][0] is True
    with Session(rig.engine) as s:
        assert rig.fp.for_call().resolve("set a timer for ten minutes").text == "Okay, 10 minutes."
        flags.set_flag(s, "g10.timers", False)
    off = rig.fp.for_call()
    assert off.resolve("set a timer for ten minutes") is None and off.resolve("cancel the timer") is None
    assert off.resolve("which timers do I have") is None
    assert off.resolve("what time is it") is not None                                # the other switches are separate
    assert len(rig.rows()) == 1                                                      # nothing set while it was off
    with Session(rig.engine) as s:
        flags.set_flag(s, "g10.timers", True)
    assert rig.fp.for_call().resolve("cancel the timer").text == "Okay, timer cancelled."


def test_a_timer_that_is_set_still_rings_when_the_switch_is_turned_off(rig):
    rig.say("set a timer for ten minutes for the pasta")
    with Session(rig.engine) as s:
        flags.set_flag(s, "g10.timers", False)
    rig.clock.advance(minutes=10)
    assert rig.timers.fire_due() == 1 and "the pasta" in rig.bell.calls[0]["body"]   # the runner does not read the switch


def test_the_language_of_the_speech_to_text_applies_to_timers_too(rig):
    assert rig.say("Stell einen Timer auf 5 Minuten", language="en") is None
    assert rig.say("set a timer for 5 minutes", language="de") is None
    assert rig.say("set a timer for 5 minutes", mode="de") is None
    assert rig.rows() == []
    assert rig.say("set a timer for 5 minutes", language="en", mode="auto") == "Okay, 5 minutes."


def test_without_a_timers_object_the_sentences_go_on_to_the_voice_model(engine):
    fp = fastpath.FastPaths(tz=TZ, now=Clock(T0), engine=engine)                     # no Timers: never set, never faked
    assert fp.rules(time_date=True, timers=True).resolve("set a timer for ten minutes") is None


def test_a_timer_needs_no_approval_and_touches_no_gate():
    """Timers are an internal action that only touches the owner (v7): the three modules import nothing from the gate, the
    executor, the approvals, the brain or Google, so no card, no external action and no model can come out of them."""
    import ast
    from pathlib import Path

    from app import timerphrase
    for mod in (fastpath, timers_mod, timerphrase):
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(("." * node.level) + (node.module or ""))
                names.update(("." * node.level) + (node.module or "") + "." + a.name for a in node.names)
        bad = {n for n in names if any(w in n for w in ("gate", "executor", "approvals", "brain", "google", "tools", "actions", "mcp"))}
        assert not bad, (mod.__name__, bad)
