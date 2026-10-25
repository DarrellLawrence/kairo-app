"""G10: a fast path's answer on the call (voicemouth.Mouth.turn). A rule's answer is a line Kairo says, like the greeting: the voice
model is not asked, Claude is not asked, the exchange is kept in the call's conversation, and the turn is counted as route "rule"."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from app import fastpath, voicestats
from app.timers import Timers
from app.voicemouth import Mouth, Turn
from tests.test_pa import TZ
from tests.test_timers import Bell, Clock, db_template, engine  # noqa: F401  (fixtures: one migrated database per file)
from tests.test_voicemouth import ITEMS, Brain, Model, Speaker, run, text, tool

WED = dt.datetime(2026, 10, 14, 14, 35, tzinfo=TZ)


class Rules:
    """Stands in for fastpath.CallRules and keeps what it was asked."""

    def __init__(self, answer=None, boom=False):
        self.answer, self.boom, self.asked = answer, boom, []

    def resolve(self, text, *, language=None, mode="auto", execute=True):
        self.asked.append((text, language, mode, execute))
        if self.boom:
            raise RuntimeError("rule broke")
        return self.answer(text) if callable(self.answer) else self.answer


def mouth(model, rules=None, brain=None, **kw):
    m = Mouth(model, ask=brain or Brain(), context="## Right now\nWednesday 14:00", items=ITEMS, name="Sam", **kw)
    m.fast = rules
    return m


def real_rules(engine, **kw):
    clock = Clock(WED)
    timers = Timers(engine, Bell(), tz=TZ, now=clock)
    fp = fastpath.FastPaths(tz=TZ, now=clock, engine=engine, timers=timers)
    return fp.rules(time_date=True, timers=True, **kw), timers


def test_a_rule_answers_the_turn_alone_and_the_voice_model_is_never_asked(engine):
    rules, _ = real_rules(engine)
    m, sp, brain = Model(), Speaker(), Brain()                       # a model with no replies: asking it would fail the test
    mo = mouth(m, rules, brain)
    r = run(mo.turn("What time is it?", "en", sp))
    assert sp.said == ["It's 2:35 PM."] and sp.fed == [] and m.calls == [] and brain.asked == []
    assert r.text == "It's 2:35 PM." and r.route == "rule" and r.asked == [] and r.failed is None and r.interrupted is False
    assert r.first_text_ms is not None and not r.filled                # no holding line: there was nothing to wait for


def test_the_call_remembers_what_was_said_so_the_next_turn_is_not_lost(engine):
    rules, _ = real_rules(engine)
    m, sp = Model(text("Not much, you?")), Speaker()
    mo = mouth(m, rules)
    run(mo.turn("What time is it?", "en", sp))
    run(mo.turn("how are you", "en", sp))                             # not a rule: the voice model, and it sees the first exchange
    roles = [(x["role"], x["content"]) for x in m.calls[0]["messages"][1:]]
    assert roles[0][0] == "user" and roles[0][1].endswith("What time is it?")
    assert roles[1] == ("assistant", "It's 2:35 PM.") and roles[2][0] == "user" and roles[2][1].endswith("how are you")
    assert mo.transcript == ["Owner: What time is it?", "Kairo (voice): It's 2:35 PM.", "Owner: how are you", "Kairo (voice): Not much, you?"]


def test_a_sentence_no_rule_answers_goes_to_the_voice_model_as_before(engine):
    rules, _ = real_rules(engine)
    m, sp = Model(tool("when is my train"), text("At six.")), Speaker()
    r = run(mouth(m, rules).turn("what time is my train", "en", sp))
    assert len(m.calls) == 2 and r.route == "claude" and r.asked == ["when is my train"]
    m2, sp2 = Model(text("Sure! ", "Here you go.")), Speaker()
    r2 = run(mouth(m2, rules).turn("Tell me something nice", "en", sp2))
    assert len(m2.calls) == 1 and r2.route == "talk" and sp2.heard == "Sure! Here you go."


def test_a_german_question_is_answered_in_german_and_the_calls_language_setting_is_respected(engine):
    rules, _ = real_rules(engine)
    sp = Speaker()
    r = run(mouth(Model(), rules).turn("Wie spät ist es?", "de", sp))
    assert sp.said == ["Es ist 14 Uhr 35."] and r.route == "rule"
    m = Model(text("It is half past two."))
    sp2 = Speaker()
    r2 = run(mouth(m, rules, language_mode="en").turn("Wie spät ist es?", "de", sp2))      # a call set to English only
    assert m.calls and r2.route != "rule"


def test_the_rule_gets_the_speech_to_text_language_and_the_calls_language_mode_but_a_greeting_never_asks_one(engine):
    rules = Rules(None)
    m, sp = Model(text("Fine, thanks.")), Speaker()
    run(mouth(m, rules, language_mode="de").turn("what time is it", "de", sp))
    assert rules.asked == [("what time is it", "de", "de", True)] and m.calls
    rules2 = Rules(None)
    sp2 = Speaker()
    r = run(mouth(Model(), rules2).turn("hello there", "en", sp2))
    assert rules2.asked == [] and r.route == "talk" and sp2.said == ["Hey Sam."]                  # the cached greeting, as before


def test_a_rule_that_breaks_costs_nothing_the_voice_model_answers(engine):
    m, sp = Model(text("It's half past two.")), Speaker()
    r = run(mouth(m, Rules(boom=True)).turn("what time is it", "en", sp))
    assert m.calls and sp.heard == "It's half past two." and r.route != "rule" and r.failed is None


def test_without_rules_the_mouth_is_exactly_what_it_was(engine):
    m, sp = Model(text("Half past two.")), Speaker()
    r = run(mouth(m, None).turn("what time is it", "en", sp))
    assert m.calls and r.route in ("card", "talk")


def test_the_pause_does_not_start_the_voice_model_on_a_question_a_rule_will_answer(engine):
    rules, _ = real_rules(engine)
    m = Model(text("x"), text("y"))
    mo = mouth(m, rules)

    async def go():
        assert mo.speculate("What time is it?") is False             # nothing started: the rule answers
        assert mo.speculate("tell me something nice") is True        # anything else runs ahead as before
        mo.drop_speculation()
    run(go())
    assert mo.speculated == 1


def test_asking_whether_a_rule_would_answer_sets_nothing(engine):
    rules, timers = real_rules(engine)
    mo = mouth(Model(text("x")), rules)

    async def go():
        assert mo.speculate("set a timer for ten minutes") is False
    run(go())
    assert timers.active() == []                                       # the pause only asked; the turn will set it


def test_a_timer_sentence_on_a_call_sets_the_timer_and_says_so(engine):
    rules, timers = real_rules(engine)
    sp, m = Speaker(), Model()
    r = run(mouth(m, rules).turn("Set a timer for ten minutes", "en", sp))
    assert sp.said == ["Okay, 10 minutes."] and r.route == "rule" and m.calls == []
    assert [(t.seconds, t.lang) for t in timers.active()] == [(600, "en")]


def test_a_rule_turn_never_raises_an_item_and_what_claude_still_owes_is_said_after(engine):
    """The bring-up list is for the voice model's own words and the wait; a rule's line has neither."""
    rules, _ = real_rules(engine)
    mo = mouth(Model(), rules)
    sp = Speaker()
    r = run(mo.turn("What time is it?", "en", sp))
    assert r.raised == [] and r.waited == [] and mo.raised == set()


def test_the_route_is_counted_as_a_rule_answer(engine):
    from sqlmodel import Session
    voicestats.record(engine, "rule", "test")
    voicestats.record(engine, "card", "test")
    voicestats.record(engine, "bogus", "test")                         # still ignored
    with Session(engine) as s:
        got = voicestats.summary(s)
    assert (got["rule"], got["card"], got["claude"], got["talk"]) == (1, 1, 0, 0)
    assert got["questions"] == 1 and got["on_pc_pct"] == 100            # the percentages are about questions the model saw


def test_a_turn_is_a_rule_turn_only_through_its_route():
    assert Turn().route == "talk"
