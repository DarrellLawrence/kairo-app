"""G13: the speaker check labels every finished turn of a call (owner | unsure | stranger | unchecked).

The call gate (speaker.py CallGate) already stops a voice that is not the owner's from being answered at all when the
check is on. The label is the second layer: a turn that IS answered (a short turn with a weak score, a check that was
too slow) still says how sure Kairo was, and that goes to the PreToolUse hook (test_speaker_label_gate.py). Synthetic
audio only (tests/test_speaker.py's made-up voices and its pitch model). Written before the code."""

from __future__ import annotations

import asyncio
import time

import pytest

np = pytest.importorskip("numpy")

from app import speaker as sp  # noqa: E402
from app.db import make_engine  # noqa: E402
from tests.test_speaker import NEAR_HZ, OTHER_HZ, OWNER_HZ, PitchModel, pcm, taught, turn, voice  # noqa: E402


def test_the_four_labels_are_the_gates():
    from app.gate import VOICE_LABELS, VOICE_MAY_CHANGE
    assert sp.LABELS == VOICE_LABELS == ("owner", "unsure", "stranger", "unchecked")
    assert VOICE_MAY_CHANGE == {sp.OWNER, sp.UNCHECKED}


@pytest.mark.parametrize("on,ok,score,mode,want", [
    (False, True, None, "normal", "unchecked"),         # the check is off or has no voiceprint
    (False, True, 0.9, "normal", "unchecked"),
    (True, True, 0.9, "normal", "owner"),
    (True, True, sp.THRESHOLD["normal"], "normal", "owner"),
    (True, True, sp.THRESHOLD["normal"] - 0.01, "normal", "unsure"),     # a short turn that scored weakly
    (True, True, sp.THRESHOLD["strict"] - 0.01, "strict", "unsure"),
    (True, True, sp.THRESHOLD["strict"], "strict", "owner"),
    (True, True, None, "normal", "unsure"),             # the check is on and could not tell (too short, no score)
    (True, False, 0.02, "normal", "stranger"),
])
def test_label_for(on, ok, score, mode, want):
    assert sp.label_for(on=on, ok=ok, score=score, mode=mode) == want


def test_a_verdict_is_unchecked_until_a_check_says_otherwise():
    assert sp.Verdict(True, "answered", "x").label == "unchecked"


def test_the_owner_is_owner_someone_else_a_stranger(tmp_path):
    g = taught(tmp_path).gate()
    me = turn(g, (voice(2.0, OWNER_HZ, seed=2), True))
    assert (me.answer, me.label) == (True, "owner")
    other = turn(g, (voice(2.0, OTHER_HZ, seed=3), True))
    assert (other.answer, other.label) == (False, "stranger")


def test_a_short_weak_turn_is_answered_but_unsure(tmp_path):
    g = taught(tmp_path).gate()
    short = turn(g, (voice(0.6, NEAR_HZ, seed=5), True))
    assert short.answer and short.why == "short: you" and short.score < sp.THRESHOLD["normal"]
    assert short.label == "unsure"
    too_short = turn(g, (voice(0.2, OWNER_HZ, seed=6), True))
    assert too_short.answer and too_short.score is None and too_short.label == "unsure"


def test_a_quiet_turn_that_is_surely_the_owner_is_owner(tmp_path):
    g = taught(tmp_path).gate()
    far_me = turn(g, (voice(2.0, OWNER_HZ, seed=6, gain=0.3 * 10 ** (-30 / 20)), True))
    assert far_me.answer and far_me.label == "owner"


def test_the_check_off_is_unchecked(tmp_path, migrated):
    """No voiceprint yet, or switched off: today's behaviour, the turn can do what it could before."""
    s = sp.Speaker(PitchModel(), tmp_path / "voiceprint.bin")
    assert turn(s.gate(), (voice(2.0, OTHER_HZ, seed=2), True)).label == "unchecked"
    s2 = taught(tmp_path, make_engine(migrated.db_url))
    s2.set_mode("off")
    v = turn(s2.gate(), (voice(2.0, OTHER_HZ, seed=3), True))
    assert v.answer and v.label == "unchecked"


def test_label_for_a_check_that_should_run_but_cannot_is_unsure():
    """broken: a voiceprint is taught and the setting is not off, but the check cannot run. That is not the owner's
    choice, so it is never unchecked: it fails closed, like the rest of the gate."""
    assert sp.label_for(on=False, ok=True, score=None, mode="off", broken=True) == "unsure"
    assert sp.label_for(on=False, ok=True, score=None, mode="off", broken=False) == "unchecked"
    assert sp.label_for(on=True, ok=True, score=0.9, mode="normal", broken=True) == "owner"      # (broken only matters when off)


BREAKAGES = {
    "model failed its load self-check": lambda s, m: setattr(s.model, "failed", "ORT self-check failed"),
    "model file missing after an update": lambda s, m: setattr(s.model, "installed", lambda: False),
    "voice packages gone": lambda s, m: m.setattr(sp, "np", None),
    "voiceprint made with another model": lambda s, m: m.setattr(sp, "MODEL_NAME", "another-model.onnx"),
    "voiceprint unreadable": lambda s, m: m.setattr("app.secretbox.load", lambda path: (_ for _ in ()).throw(OSError("denied"))),
}


@pytest.mark.parametrize("what", list(BREAKAGES))
def test_a_taught_voice_whose_check_cannot_run_is_unsure_not_unchecked(tmp_path, monkeypatch, what):
    """Review finding: the protection must not turn itself off exactly when the check breaks. The turn is still answered
    (a broken check never costs the call, as before) but it changes nothing."""
    s = taught(tmp_path)
    assert s.check_state()[0] is True
    BREAKAGES[what](s, monkeypatch)
    s._print = None                                       # (the voiceprint is read again)
    on, why = s.check_state()
    assert on is False and why, what
    assert s.check_status() == ("broken", why)
    v = turn(s.gate(), (voice(2.0, OTHER_HZ, seed=3), True))
    assert v.answer and v.label == "unsure", what
    short = turn(s.gate(), (voice(0.2, OWNER_HZ, seed=4), True))
    assert short.answer and short.label == "unsure", what


@pytest.mark.parametrize("what", list(BREAKAGES))
def test_the_same_breakage_with_the_check_switched_off_is_unchecked(tmp_path, monkeypatch, migrated, what):
    """The owner chose off: that stays unchecked (and diag warns)."""
    s = taught(tmp_path, make_engine(migrated.db_url))
    s.set_mode("off")
    BREAKAGES[what](s, monkeypatch)
    s._print = None
    assert s.check_status()[0] == "off"
    v = turn(s.gate(), (voice(2.0, OTHER_HZ, seed=3), True))
    assert v.answer and v.label == "unchecked", what


def test_nothing_taught_is_unchecked_whatever_else_is_broken(tmp_path, monkeypatch):
    """No voiceprint: the owner has not turned the check on yet. Unchecked, as before, with the model missing too."""
    s = sp.Speaker(PitchModel(), tmp_path / "voiceprint.bin")
    s.model.installed = lambda: False
    monkeypatch.setattr(sp, "np", None)
    assert s.check_status()[0] == "off"
    assert turn(s.gate(), (voice(2.0, OTHER_HZ, seed=4), True)).label == "unchecked"


def test_a_broken_check_with_a_different_voice_is_stopped_by_the_gate(tmp_path):
    """The reviewer's probe, end to end: voiceprint taught, the model failed, a different voice is answered as before
    but the gate (the one the hook runs) refuses everything that changes anything."""
    from app.gate import GateContext, load_gate
    from app.settings import REPO_ROOT
    s = taught(tmp_path)
    s.model.failed = "ORT self-check failed"
    v = turn(s.gate(), (voice(2.0, OTHER_HZ, seed=3), True))
    assert v.answer and v.label == "unsure"
    gate = load_gate(REPO_ROOT / "config")
    ctx = GateContext(False, "owner_chat", 0, 0, voice=v.label)
    assert gate.decide("mcp__kairo__tasks_create", {"title": "x"}, ctx).verdict == "deny"
    proposal = {"action": "gmail.send", "args": {"to": ["x@example.com"], "subject": "s", "body": "b"}, "reason": "r"}
    assert gate.decide("mcp__kairo__propose_action", proposal, ctx).verdict == "deny"
    assert gate.decide("mcp__kairo__tasks_list", {}, ctx).verdict == "allow"          # reading stays


def test_a_broken_or_slow_check_with_the_check_on_is_unsure(tmp_path, monkeypatch):
    """The turn is still answered (the check is a filter, not a wall) but it cannot change anything: the check was on and
    could not say it was the owner."""
    s = taught(tmp_path)

    def broken(samples):
        raise RuntimeError("CUDA went away")
    s.model.embed = broken
    v = turn(s.gate(), (voice(2.0, OTHER_HZ, seed=3), True))
    assert v.answer and v.label == "unsure"
    monkeypatch.setattr(sp, "WAIT_MAX_S", 0.05)
    s.measure = lambda pcm, on: time.sleep(0.5)
    late = turn(s.gate(), (voice(1.0), True))
    assert late.answer and late.why == "slow" and late.label == "unsure"


def test_a_slow_check_that_is_broken_is_unsure_and_one_that_is_off_is_unchecked(tmp_path, monkeypatch, migrated):
    monkeypatch.setattr(sp, "WAIT_MAX_S", 0.05)
    s = taught(tmp_path)
    s.model.failed = "the model could not start"
    s.measure = lambda pcm, on: time.sleep(0.5)
    late = turn(s.gate(), (voice(1.0), True))
    assert late.answer and late.why == "slow" and late.label == "unsure"
    s2 = taught(tmp_path / "b", make_engine(migrated.db_url))
    s2.set_mode("off")
    s2.measure = lambda pcm, on: time.sleep(0.5)
    assert turn(s2.gate(), (voice(1.0), True)).label == "unchecked"


def test_a_slow_check_with_the_check_off_is_unchecked(tmp_path, monkeypatch):
    s = sp.Speaker(PitchModel(), tmp_path / "voiceprint.bin")          # not taught: off
    monkeypatch.setattr(sp, "WAIT_MAX_S", 0.05)
    s.measure = lambda pcm, on: time.sleep(0.5)
    late = turn(s.gate(), (voice(1.0), True))
    assert late.answer and late.why == "slow" and late.label == "unchecked"


def test_a_check_that_crashes_while_judging_is_unsure(tmp_path, monkeypatch):
    s = taught(tmp_path)
    g = s.gate()

    def boom(measured):
        raise ValueError("bug")
    monkeypatch.setattr(g, "judge", boom)
    v = turn(g, (voice(2.0, OWNER_HZ, seed=2), True))
    assert v.answer and v.why == "error" and v.label == "unsure"


def test_a_turn_ignored_for_being_too_far_is_not_a_confirmed_owner(tmp_path):
    g = sp.Speaker(PitchModel(), tmp_path / "voiceprint.bin").gate()
    turn(g, (voice(2, seed=1), True))
    turn(g, (voice(2, seed=2), True))
    far = turn(g, (voice(2, seed=3, gain=0.3 * 10 ** (-30 / 20)), True))
    assert (far.answer, far.outcome) == (False, "too_quiet") and far.label == "unsure"


def test_the_label_is_not_in_the_record_or_the_log(tmp_path, migrated, caplog):
    """Voice speed keeps time, reason, score and level; the label rides on the verdict only."""
    assert "label" not in sp.COLUMNS
    s = taught(tmp_path, make_engine(migrated.db_url))
    caplog.set_level("DEBUG")
    v = turn(s.gate(), (voice(2.0, OTHER_HZ, seed=3), True))
    assert v.label == "stranger" and "stranger" not in v.log_line()


# ---------------------------------------------------------------- which label goes with which turn (WebRTC: gate.answer, then answer())

def run(coro):
    return asyncio.run(coro)


def decided(g, *specs):
    """specs: (audio, words) turns, each finished through gate.answer() like voicertc does; returns the bools."""
    out = []
    for x, words in specs:
        g.words(g.hear(pcm(x)), words)
        out.append(run(g.answer()))
    return out


def test_each_answered_turn_takes_the_label_of_its_own_verdict(tmp_path):
    g = taught(tmp_path).gate()
    assert decided(g, (voice(2.0, OWNER_HZ, seed=2), True)) == [True]
    assert g.take_label() == "owner"
    assert decided(g, (voice(0.6, NEAR_HZ, seed=5), True)) == [True]
    assert g.take_label() == "unsure"
    assert decided(g, (voice(2.0, OWNER_HZ, seed=3), True)) == [True]
    assert g.take_label() == "owner"                                   # the unsure one did not stick


def test_an_ignored_turn_leaves_nothing_to_take(tmp_path):
    g = taught(tmp_path).gate()
    assert decided(g, (voice(2.0, OTHER_HZ, seed=3), True)) == [False]
    assert g.take_label() == "unsure"          # nothing was answered, so nothing should ask: fail closed, never owner


def test_when_two_turns_were_decided_before_one_answer_started_the_least_trusted_wins(tmp_path):
    """No ordering of the callbacks can make a turn more trusted than its own verdict."""
    g = taught(tmp_path).gate()
    decided(g, (voice(0.6, NEAR_HZ, seed=5), True), (voice(2.0, OWNER_HZ, seed=2), True))
    assert g.take_label() == "unsure"
    assert g.take_label() == "unsure"          # and the second answer() finds nothing left: closed, not open


def test_the_waiting_labels_are_bounded(tmp_path):
    g = taught(tmp_path).gate()
    for i in range(40):
        g._queue_label("owner")
    assert len(g._answered) <= 16
    g._queue_label("unsure")
    assert g.take_label() == "unsure" and g.take_label() == "unsure"


def test_the_old_line_reads_the_label_from_the_verdict_and_leaves_the_waiting_list_empty(tmp_path):
    g = taught(tmp_path).gate()
    g.words(g.hear(pcm(voice(2.0, OWNER_HZ, seed=2))), True)
    v = run(g.decide())
    assert v.label == "owner" and g._answered == []


def test_the_least_trusted_label_wins():
    from app.speaker_label import worst
    assert worst() is None and worst(None) is None
    assert worst("owner", "owner") == "owner"
    assert worst("owner", "unsure") == "unsure"
    assert worst("unsure", "stranger", "owner") == "stranger"
    assert worst("owner", "unchecked") == "unchecked"
    assert worst("owner", "root") == "root"                  # a label nobody knows is the worst: the gate denies it


def test_an_unsure_turn_that_was_answered_is_in_the_log_without_words(tmp_path, caplog):
    """How often the owner's own requests are unsure is read from the log (voice plan P19), so it must be there."""
    caplog.set_level("INFO", logger="kairo.speaker")
    g = taught(tmp_path).gate()
    decided(g, (voice(0.6, NEAR_HZ, seed=5), True))
    assert "answered but not confirmed as you" in caplog.text and "short: you" in caplog.text
    caplog.clear()
    decided(g, (voice(2.0, OWNER_HZ, seed=2), True))
    assert "not confirmed" not in caplog.text                          # an owner turn is not logged
