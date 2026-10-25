"""G13, review finding: the owner's short answer to an item Kairo raised on the call must not be lost silently.

A short reply ("yes", "done", "later") is the typical turn the speaker check cannot confirm (label unsure). It used to
go to Claude as a silent report ("record it now, say nothing back"); the hook then refused the change, the reply
was already marked as reported, nothing retried it and nobody told the owner. Now a reply that was said under a label
that may not change things is not sent: the owner is told (a push) to say it again or mark it in the app, and the next
reply to the item is taken as its answer again. A confirmed reply goes as before. Written before the code."""

from __future__ import annotations

import pytest

from app import speaker_label
from tests.test_voicemouth import LEA, Brain, Model, Speaker, mouth, run, text, tool

WEAK = ("unsure", "stranger")
STRONG = ("owner", "unchecked")


def may_record(label):
    """What main.py says: owner and unchecked (and no label at all) may change things; the rest may not."""
    return label is None or label in STRONG


def first_turns(m, sent, held, **kw):
    """A call where LEA is raised on the first turn; returns the mouth."""
    mo = mouth(m, kw.pop("brain", Brain(delay=0.2)), items=[LEA], wait_after_s=0.02, report=lambda *r: sent.append(r),
               may_record=may_record, on_held=held.append, **kw)
    run(mo.turn("Is Friday free?", "en", Speaker()))
    return mo


def turn(mo, words, label):
    with speaker_label.scope(label):
        return run(mo.turn(words, "en", Speaker()))


@pytest.mark.parametrize("label", STRONG)
def test_a_confirmed_reply_goes_to_claude_as_before_and_nobody_is_told_anything(label):
    sent, held = [], []
    mo = first_turns(Model(tool("is Friday free?"), text("Free after 4."), text("Nice, noted.")), sent, held)
    turn(mo, "Yeah I told her Friday works.", label)
    assert sent == [(LEA, "Quick one: Did you answer Lea about Friday yet?", "Yeah I told her Friday works.")]
    assert held == []


@pytest.mark.parametrize("label", WEAK)
def test_a_reply_the_check_could_not_confirm_is_not_sent_and_the_owner_is_told(label):
    sent, held = [], []
    mo = first_turns(Model(tool("is Friday free?"), text("Free after 4."), text("Nice, noted.")), sent, held)
    turn(mo, "Yes", label)
    assert sent == []                                   # the hook would refuse it: do not pretend it was recorded
    assert held == [LEA]                                # ... and say so (main.py pushes)
    assert mo.reports() == []                           # nothing is left over that a later turn would send under another label


def test_the_next_reply_to_the_item_is_taken_as_its_answer_again():
    """"Say it again" has to work: the item is open for an answer once more, and a confirmed answer is sent."""
    sent, held = [], []
    mo = first_turns(Model(tool("is Friday free?"), text("Free after 4."), text("Hm."), text("Nice, noted.")), sent, held)
    turn(mo, "Yes", "unsure")
    assert sent == [] and held == [LEA]
    turn(mo, "Yes I told Lea that Friday works for me.", "owner")
    assert sent == [(LEA, "Quick one: Did you answer Lea about Friday yet?", "Yes I told Lea that Friday works for me.")]
    assert len(held) == 1


def test_the_owner_is_told_once_per_item_not_at_every_weak_turn():
    sent, held = [], []
    mo = first_turns(Model(tool("is Friday free?"), text("Free after 4."), text("Hm."), text("Hm."), text("Hm.")), sent, held)
    turn(mo, "Yes", "unsure")
    turn(mo, "yeah", "unsure")                          # said again, still not confirmed
    turn(mo, "ok", "unsure")
    assert sent == [] and held == [LEA]


def test_a_reply_said_under_a_weak_label_is_never_sent_later_under_a_good_one():
    """No laundering: the words keep the label they were said with, whatever turn would carry them. Only words the owner
    says afterwards, confirmed, count as the answer."""
    sent, held = [], []
    mo = first_turns(Model(tool("is Friday free?"), text("Free after 4."), text("Hm."), text("Fine.")), sent, held)
    turn(mo, "I did it", "stranger")
    turn(mo, "Yes I told her Friday works", "owner")
    assert [r[2] for r in sent] == ["Yes I told her Friday works"] and held == [LEA]


def test_the_answer_does_not_ride_along_with_the_next_ask_when_it_was_not_confirmed():
    sent, held, brain = [], [], Brain(delay=0.2)
    m = Model(tool("is Friday free?"), text("Free after 4."), tool("and Saturday?"), text("Saturday too."))
    mo = first_turns(m, sent, held, brain=brain)
    turn(mo, "Yes I answered her. And Saturday?", "unsure")
    assert "On this call (record it now" not in brain.asked[1][0] and brain.asked[1][0] == "and Saturday?"
    assert sent == [] and held == [LEA]


def test_a_confirmed_answer_rides_along_as_before():
    sent, held, brain = [], [], Brain(delay=0.2)
    m = Model(tool("is Friday free?"), text("Free after 4."), tool("and Saturday?"), text("Saturday too."))
    mo = first_turns(m, sent, held, brain=brain)
    turn(mo, "Yes I answered her. And Saturday?", "owner")
    assert "On this call (record it now" in brain.asked[1][0] and sent == [] and held == []


def test_a_reply_said_under_a_good_label_waits_for_a_good_turn_and_is_not_lost_to_a_weak_one():
    """A reply that was confirmed but could not be sent in its own turn (no live reporting) is not dropped by a
    later weak turn: it stays for the next report, and for the report after the call."""
    sent, held = [], []
    mo = mouth(Model(tool("is Friday free?"), text("Free after 4."), text("Nice, noted.")), Brain(delay=0.2), items=[LEA],
               wait_after_s=0.02, may_record=may_record, on_held=held.append)           # no report= : collected for after the call
    run(mo.turn("Is Friday free?", "en", Speaker()))
    turn(mo, "Yeah I told her Friday works.", "owner")
    assert [r[2] for r in mo.reports()] == ["Yeah I told her Friday works."] and held == []
    assert mo.reply_label("L1") == "owner" and mo.reply_label("L99") is None


def test_a_mouth_with_no_policy_does_what_it_always_did():
    sent = []
    mo = mouth(Model(tool("is Friday free?"), text("Free after 4."), text("Nice, noted.")), Brain(delay=0.2), items=[LEA],
               wait_after_s=0.02, report=lambda *r: sent.append(r))
    run(mo.turn("Is Friday free?", "en", Speaker()))
    turn(mo, "Yes", "unsure")                           # nobody told the mouth that this label matters
    assert len(sent) == 1


def test_a_push_that_fails_never_costs_the_call():
    sent = []

    def boom(item):
        raise RuntimeError("no push service")
    mo = mouth(Model(tool("is Friday free?"), text("Free after 4."), text("Nice, noted.")), Brain(delay=0.2), items=[LEA],
               wait_after_s=0.02, report=lambda *r: sent.append(r), may_record=may_record, on_held=boom)
    run(mo.turn("Is Friday free?", "en", Speaker()))
    r = turn(mo, "Yes", "unsure")
    assert r.text == "Nice, noted." and sent == []


def test_a_policy_that_cannot_answer_holds_the_reply_it_fails_closed():
    sent, held = [], []

    def broken(label):
        raise RuntimeError("database is locked")
    mo = mouth(Model(tool("is Friday free?"), text("Free after 4."), text("Nice, noted.")), Brain(delay=0.2), items=[LEA],
               wait_after_s=0.02, report=lambda *r: sent.append(r), may_record=broken, on_held=held.append)
    run(mo.turn("Is Friday free?", "en", Speaker()))
    r = turn(mo, "Yes I did it", "owner")
    assert r.text == "Nice, noted." and sent == [] and held == [LEA]
