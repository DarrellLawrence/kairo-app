"""G13: the label of a voice turn is written to the database BEFORE Claude runs, and removed when the run ends.

app/speaker_label.py keeps it (a ContextVar carries it from the call's answer to the brain run; VoiceTurns writes the
row the PreToolUse hook and the gate read). The model never sees or sets it. Written before the code."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading

import pytest
from sqlmodel import Session

from app import flags
from app import speaker_label as sl
from app.brain import BrainEvent, EventKind
from app.chat import BrainRunner
from app.db import SettingKV, make_engine
from app.gate_context import VOICE_TURN_KEY, read_context

FLAG = "g13.speaker_label_gate"


@pytest.fixture
def db(migrated):
    return make_engine(migrated.db_url)


def row(db):
    with Session(db) as s:
        r = s.get(SettingKV, sl.KEY)
        return None if r is None else r.value


def put(db, key, value):
    with Session(db) as s:
        r = s.get(SettingKV, key) or SettingKV(key=key)
        r.value = value
        s.add(r)
        s.commit()


def put_turn(db, value):
    put(db, sl.KEY, value)


def seen_by_the_hook(db) -> str | None:
    return read_context(db, "owner_chat", model_turn=True).voice


def test_the_gate_and_the_recorder_use_one_row():
    assert sl.KEY == VOICE_TURN_KEY == "voice.turn"
    assert sl.FLAG == FLAG


# ---------------------------------------------------------------- the label of the turn that is being handled

def test_no_label_outside_a_scope_and_it_is_put_back():
    assert sl.current() is None
    with sl.scope("unsure"):
        assert sl.current() == "unsure"
        with sl.scope("owner"):
            assert sl.current() == "owner"
        assert sl.current() == "unsure"
    assert sl.current() is None


def test_scope_puts_the_label_back_after_an_error():
    with pytest.raises(RuntimeError):
        with sl.scope("stranger"):
            raise RuntimeError("x")
    assert sl.current() is None


@pytest.mark.parametrize("bad", ["", "root", "Owner", 3, ["owner"]])
def test_a_label_nobody_knows_is_refused_at_the_door(bad):
    with pytest.raises(ValueError):
        with sl.scope(bad):
            pass


def test_the_label_follows_the_turn_into_tasks_and_worker_threads_it_starts():
    async def go():
        with sl.scope("unsure"):
            in_task = await asyncio.get_running_loop().create_task(_read())
            in_thread = await asyncio.to_thread(sl.current)
        return in_task, in_thread, sl.current()

    async def _read():
        return sl.current()
    assert asyncio.run(go()) == ("unsure", "unsure", None)


def test_two_turns_at_once_keep_their_own_labels():
    async def turn(label, other_started, me_started):
        with sl.scope(label):
            me_started.set()
            await other_started.wait()
            await asyncio.sleep(0)
            return sl.current()

    async def go():
        a, b = asyncio.Event(), asyncio.Event()
        return await asyncio.gather(turn("unsure", b, a), turn("owner", a, b))
    assert asyncio.run(go()) == ["unsure", "owner"]


# ---------------------------------------------------------------- the row

def test_begin_writes_the_label_and_end_removes_it(db):
    turns = sl.VoiceTurns(db)
    run = turns.begin("unsure")
    assert run and seen_by_the_hook(db) == "unsure"
    rec = json.loads(row(db))
    assert rec["label"] == "unsure" and rec["run"] == run and rec["at"]
    turns.end(run)
    assert row(db) is None and seen_by_the_hook(db) is None


def test_the_record_says_which_conversation_and_kind_of_run_and_no_words(db):
    run = sl.VoiceTurns(db).begin("unsure", thread_id=7, purpose="call")
    rec = json.loads(row(db))
    assert rec["thread"] == 7 and rec["purpose"] == "call" and rec["run"] == run
    assert set(rec) == {"label", "run", "thread", "purpose", "at"}


def test_the_runner_hands_over_the_conversation_and_purpose(db):
    class Peek(Brain):
        def run(self, prompt, *, session_id, system, tools, origin="owner_chat", **kw):
            self.during.append(json.loads(row(db)))
            yield BrainEvent(EventKind.result, text="ok")
    brain = Peek(db)
    runner = BrainRunner(brain)
    runner.voice_turns = sl.VoiceTurns(db)

    async def go():
        with sl.scope("owner"):
            return [e async for e in runner.stream("hi", session_id=None, system="s", tools=[], thread_id=5, purpose="call")]
    asyncio.run(go())
    assert brain.during[0]["thread"] == 5 and brain.during[0]["purpose"] == "call" and brain.during[0]["label"] == "owner"


def test_begin_without_a_label_clears_what_a_crash_left(db):
    put_turn(db, json.dumps({"label": "stranger", "run": "old", "at": "2026-10-24T10:00:00+00:00"}))
    assert sl.VoiceTurns(db).begin(None) is None
    assert row(db) is None


def test_begin_replaces_a_record_that_cannot_be_read(db):
    put_turn(db, "garbage")
    sl.VoiceTurns(db).begin("owner")
    assert seen_by_the_hook(db) == "owner"


def test_end_only_removes_its_own_record(db):
    turns = sl.VoiceTurns(db)
    first = turns.begin("unsure")
    second = turns.begin("owner")                       # a later run took over (it can't overlap, but be sure)
    turns.end(first)
    assert seen_by_the_hook(db) == "owner"
    turns.end(second)
    assert row(db) is None
    turns.end(None)                                     # nothing to end: fine


def test_the_flag_off_writes_nothing_and_clears(db):
    put(db, flags.PREFIX + FLAG, "0")
    turns = sl.VoiceTurns(db)
    assert turns.begin("stranger") is None and row(db) is None
    put_turn(db, json.dumps({"label": "stranger", "run": "old", "at": "x"}))
    assert turns.begin("stranger") is None and row(db) is None        # and a leftover is gone


def test_a_label_nobody_knows_is_never_written(db):
    with pytest.raises(ValueError):
        sl.VoiceTurns(db).begin("root")
    assert row(db) is None


def test_the_model_cannot_write_the_row(db):
    """settings_set writes one key of the owner's preferences, not arbitrary settings rows."""
    from app import prefs
    with Session(db) as s:
        for key in (sl.KEY, "voice.turn", flags.PREFIX + FLAG, "paused"):
            with pytest.raises(prefs.PrefsError):
                prefs.set_key(s, key, json.dumps({"label": "owner"}))
    assert row(db) is None


# ---------------------------------------------------------------- the brain run (chat.BrainRunner)

class Brain:
    """Records what the database says while it runs, like the hook would see it."""

    name = "fake"

    def __init__(self, db, fail=False):
        self.db, self.fail, self.during, self.prompts = db, fail, [], []
        self.gate = threading.Event()
        self.entered = threading.Event()

    def run(self, prompt, *, session_id, system, tools, origin="owner_chat", **kw):
        self.prompts.append((prompt, system))
        self.during.append(seen_by_the_hook(self.db))
        self.entered.set()
        if self.fail:
            raise RuntimeError("the brain broke")
        self.gate.wait(5) if prompt.startswith("hold") else None
        self.during.append(seen_by_the_hook(self.db))
        yield BrainEvent(EventKind.result, text="ok")


def run_turn(runner, text, label=None):
    async def go():
        with sl.scope(label) if label is not None else contextlib.nullcontext():
            return [e async for e in runner.stream(text, session_id=None, system="sys", tools=[])]
    return asyncio.run(go())


def test_the_label_is_in_the_database_before_claude_runs_and_gone_after(db):
    brain = Brain(db)
    runner = BrainRunner(brain)
    runner.voice_turns = sl.VoiceTurns(db)
    events = run_turn(runner, "remind me", "unsure")
    assert events[-1].text == "ok"
    assert brain.during == ["unsure", "unsure"]          # there when the brain started, still there at the end
    assert row(db) is None                               # and removed once it is over


def test_the_model_is_never_told(db):
    with_label, without = Brain(db), Brain(db)
    a, b = BrainRunner(with_label), BrainRunner(without)
    a.voice_turns = b.voice_turns = sl.VoiceTurns(db)
    run_turn(a, "remind me", "stranger")
    run_turn(b, "remind me")
    assert with_label.prompts == without.prompts == [("remind me", "sys")]


def test_a_turn_with_no_label_starts_clean(db):
    """A typed message after a crash that left a stranger's record must not be blocked by it."""
    put_turn(db, json.dumps({"label": "stranger", "run": "old", "at": "2026-10-24T10:00:00+00:00"}))
    brain = Brain(db)
    runner = BrainRunner(brain)
    runner.voice_turns = sl.VoiceTurns(db)
    run_turn(runner, "typed")
    assert brain.during == [None, None] and row(db) is None


def test_the_record_is_removed_when_the_brain_breaks(db):
    brain = Brain(db, fail=True)
    runner = BrainRunner(brain)
    runner.voice_turns = sl.VoiceTurns(db)
    events = run_turn(runner, "remind me", "unsure")
    assert events[-1].kind is EventKind.error and "the brain broke" in events[-1].text
    assert brain.during == ["unsure"] and row(db) is None


def test_if_the_label_cannot_be_written_claude_does_not_run(db):
    """Fail closed: a voice turn Kairo cannot label must not reach Claude with tools."""
    brain = Brain(db)
    runner = BrainRunner(brain)

    class Broken:
        def begin(self, label, **kw):
            raise RuntimeError("database is locked")

        def end(self, run):
            raise AssertionError("nothing began")
    runner.voice_turns = Broken()
    events = run_turn(runner, "remind me", "owner")
    assert brain.prompts == [] and events[-1].kind is EventKind.error
    assert "database is locked" in events[-1].text


def test_a_runner_without_the_recorder_is_what_it_was(db):
    brain = Brain(db)
    runner = BrainRunner(brain)
    assert runner.voice_turns is None
    run_turn(runner, "remind me", "stranger")
    assert brain.during == [None, None] and row(db) is None


def test_a_waiting_turn_never_gets_the_label_of_the_one_running(db):
    """One brain call at a time: B (owner) waits for A (unsure); A's tool calls see unsure to the end, B's see owner."""
    brain = Brain(db)
    runner = BrainRunner(brain)
    runner.voice_turns = sl.VoiceTurns(db)
    out = {}

    def turn(name, text, label):
        out[name] = run_turn(runner, text, label)

    a = threading.Thread(target=turn, args=("a", "hold a", "unsure"))
    a.start()
    assert brain.entered.wait(5)
    b = threading.Thread(target=turn, args=("b", "plain b", "owner"))
    b.start()
    for _ in range(40):                                  # b is queued behind a, and has written nothing yet
        if runner._owners_waiting:
            break
        threading.Event().wait(0.05)
    assert seen_by_the_hook(db) == "unsure"
    brain.gate.set()
    a.join(10)
    b.join(10)
    assert brain.during == ["unsure", "unsure", "owner", "owner"]
    assert row(db) is None


def test_a_job_run_is_not_touched(db):
    """Jobs have no tools and use ask_sync; their hold never writes the row."""
    from app.chat import ask_sync

    class Quiet(Brain):
        def run(self, prompt, *, session_id, system, tools, origin="owner_chat", **kw):
            self.during.append(row(db))
            yield BrainEvent(EventKind.result, text="{}")
    brain = Quiet(db)
    runner = BrainRunner(brain)
    runner.voice_turns = sl.VoiceTurns(db)
    put_turn(db, json.dumps({"label": "unsure", "run": "call", "at": "x"}))      # a call is going on
    assert ask_sync(runner, "triage", system="s").ok
    assert brain.during == [json.dumps({"label": "unsure", "run": "call", "at": "x"})]
    assert row(db) is not None                                               # the call's record stays
