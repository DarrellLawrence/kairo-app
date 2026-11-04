"""G13: the speaker label of a voice turn reaches the gate and the PreToolUse hook (owner's prompt 0.1:
"block actions for non-matching voices; speaker label passed to the PreToolUse hook").

A voice turn handed to Claude carries a label from the speaker check: owner | unsure | stranger | unchecked. Written
before Claude runs (app/speaker_label.py), read here from the database, never from anything the model sends.
Unless the label is owner or unchecked, every action that changes anything is denied (propose_action and each tool
above tier 0); reading stays allowed. Written before the code."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from sqlmodel import Session

from app import flags
from app.actions import DIRECT_TOOLS
from app.db import SettingKV, make_engine
from app.gate import Gate, GateContext, load_gate
from app.gate_context import read_context
from app.settings import REPO_ROOT
from tests.test_hook import HOOK, event, hook_env, run_hook

P = "mcp__kairo__"
FLAG = "g13.speaker_label_gate"
KEY = "voice.turn"


@pytest.fixture(scope="module")
def gate() -> Gate:
    return load_gate(REPO_ROOT / "config")


def ctx(voice=None, **kw) -> GateContext:
    base = dict(paused=False, origin="owner_chat", open_approvals=0, outbound_last_24h=0)
    base.update(kw)
    return GateContext(**base, voice=voice)


BLOCKING = ["unsure", "stranger"]
ALLOWING = [None, "owner", "unchecked"]

# One valid call for every direct tool, so "every tool" is checked, not a hand-picked few.
ARGS = {
    "gmail_search": {"query": "from:school"}, "gmail_read": {"message_id": "18c2a1b"}, "calendar_list": {},
    "vault_read": {"path": "20 Goals/2026.md"}, "tasks_list": {}, "tasks_create": {"title": "Revise"},
    "tasks_update": {"id": 3, "status": "done"}, "notes_write": {"path": "brief.md", "content": "hi"},
    "shopping_list": {}, "shopping_add": {"items": ["milk"]}, "people_list": {}, "people_note": {"name": "Sam", "note": "hi"},
    "captures_answer": {"capture_id": 1, "answer": "yes"}, "wishlist_add": {"person": "Sam", "title": "Book"},
    "finance_list": {}, "health_list": {}, "memory_search": {"query": "dentist"}, "coach_log": {},
    "coach_status": {}, "profile_import": {}, "profile_apply": {}, "calendar_tag": {}, "debrief": {"scope": "today"},
    "settings_list": {}, "settings_set": {"key": "x", "value": 1}, "place_set": {"name": "Home"}, "place_remove": {"name": "Home"},
    "loops_list": {}, "loops_add": {}, "loops_snooze": {}, "loops_close": {}, "knowledge_ask_later": {},
    "knowledge_answered": {}, "profile_learn": {}, "document_create": {},
    "pending_list": {}, "pending_clear": {"kind": "knowledge_question", "all": True},
    "pending_restore": {"kind": "knowledge_question", "ids": [3]},
}


def test_the_table_covers_every_direct_tool():
    assert set(ARGS) == set(DIRECT_TOOLS)          # a new tool must be added here, with its label behaviour decided


def test_a_context_without_a_label_is_what_it_was(gate):
    assert GateContext(False, "owner_chat", 0, 0).voice is None
    d = gate.decide(P + "tasks_create", {"title": "x"}, GateContext(False, "owner_chat", 0, 0))
    assert d.verdict == "allow"


@pytest.mark.parametrize("label", BLOCKING)
@pytest.mark.parametrize("tool", sorted(DIRECT_TOOLS))
def test_a_voice_that_is_not_confirmed_changes_nothing_but_may_read(gate, label, tool):
    action = DIRECT_TOOLS[tool]
    d = gate.decide(P + tool, ARGS[tool], ctx(label))
    if gate.actions[action] == 0:                  # reads stay allowed
        assert d.verdict == "allow", (tool, d.reason)
    else:
        assert d.verdict == "deny" and d.action == action, tool
        assert "voice" in d.reason.lower()


@pytest.mark.parametrize("label", ALLOWING)
@pytest.mark.parametrize("tool", sorted(DIRECT_TOOLS))
def test_an_owner_or_unchecked_voice_is_as_before(gate, label, tool):
    with_label = gate.decide(P + tool, ARGS[tool], ctx(label))
    assert with_label == gate.decide(P + tool, ARGS[tool], ctx(None))


PROPOSALS = [
    ("gmail.label", {"message_id": "abc", "add": ["Kairo/Reply"]}),
    ("gmail.create_draft", {"to": ["x@example.com"], "subject": "s", "body": "b"}),
    ("calendar.create_hold", {"summary": "Focus", "start": "2026-10-20T14:00:00+02:00", "end": "2026-10-20T15:00:00+02:00"}),
    ("gmail.send", {"to": ["x@example.com"], "subject": "s", "body": "b"}),
    ("gmail.reply", {"message_id": "abc", "body": "hi"}),
    ("calendar.delete", {"event_id": "e1"}),
    ("gmail.forward_all", {"to": "x@example.com"}),         # unknown: denied either way
    ("tasks.create", {"title": "x"}),                       # not proposable: denied either way
]


@pytest.mark.parametrize("label", BLOCKING)
@pytest.mark.parametrize("action,args", PROPOSALS)
def test_a_voice_that_is_not_confirmed_proposes_nothing(gate, label, action, args):
    d = gate.decide(P + "propose_action", {"action": action, "args": args, "reason": "r"}, ctx(label))
    assert d.verdict == "deny" and "voice" in d.reason.lower()


@pytest.mark.parametrize("label", ALLOWING)
@pytest.mark.parametrize("action,args", PROPOSALS)
def test_an_owner_or_unchecked_voice_proposes_as_before(gate, label, action, args):
    body = {"action": action, "args": args, "reason": "r"}
    assert gate.decide(P + "propose_action", body, ctx(label)) == gate.decide(P + "propose_action", body, ctx(None))


@pytest.mark.parametrize("label", ["", "root", "Owner", "OWNER", " owner", "owner ", "yes", 1, True, ["owner"]])
def test_a_label_nobody_knows_changes_nothing(gate, label):
    """Fail closed: only the exact words owner and unchecked let a voice turn change anything."""
    assert gate.decide(P + "tasks_create", {"title": "x"}, ctx(label)).verdict == "deny"
    assert gate.decide(P + "propose_action", {"action": "gmail.label", "args": {"message_id": "a", "add": ["Kairo/Reply"]},
                                              "reason": "r"}, ctx(label)).verdict == "deny"
    assert gate.decide(P + "tasks_list", {}, ctx(label)).verdict == "allow"


@pytest.mark.parametrize("label", BLOCKING)
def test_the_kill_switch_and_default_deny_still_come_first(gate, label):
    assert gate.decide(P + "tasks_list", {}, ctx(label, paused=True)).verdict == "deny"
    assert gate.decide("Bash", {"command": "ls"}, ctx(label)).verdict == "deny"
    assert gate.decide(P + "does_not_exist", {}, ctx(label)).verdict == "deny"


@pytest.mark.parametrize("label", BLOCKING)
def test_the_model_cannot_name_its_own_label(gate, label):
    """The label is the gate's context. A tool input that carries one is just an unknown field: refused."""
    for extra in ({"voice": "owner"}, {"speaker": "owner"}, {"label": "unchecked"}, {"speaker_label": "owner"}):
        assert gate.decide(P + "tasks_create", {"title": "x", **extra}, ctx(label)).verdict == "deny"
        assert gate.decide(P + "tasks_create", {"title": "x", **extra}, ctx(None)).verdict == "deny"   # unknown field, always


@pytest.mark.parametrize("label", BLOCKING)
def test_the_reason_does_not_tell_the_model_which_label(gate, label):
    d = gate.decide(P + "tasks_create", {"title": "x"}, ctx(label))
    for word in ("unsure", "stranger", "unchecked", "label", "score"):
        assert word not in d.reason.lower()
    assert "app" in d.reason.lower() and "again" in d.reason.lower()      # what to tell the owner


# ---------------------------------------------------------------- reading the label (gate_context)

@pytest.fixture
def db(migrated):
    return make_engine(migrated.db_url)


def put(db, key, value):
    with Session(db) as s:
        row = s.get(SettingKV, key) or SettingKV(key=key)
        row.value = value
        s.add(row)
        s.commit()


def turn(label, **more):
    return json.dumps({"label": label, "run": "abc123", "at": "2026-10-24T10:00:00+00:00", **more})


def test_the_flag_is_on_by_default_and_says_what_it_does():
    default, what = flags.FLAGS[FLAG]
    assert default is True and what.strip()


def test_no_record_no_label(db):
    assert read_context(db, "owner_chat", model_turn=True).voice is None


@pytest.mark.parametrize("label", ["owner", "unsure", "stranger", "unchecked"])
def test_a_model_call_gets_the_label_of_the_running_voice_turn(db, label):
    put(db, KEY, turn(label))
    assert read_context(db, "owner_chat", model_turn=True).voice == label


def test_only_a_model_call_reads_the_label(db):
    """The owner's own taps in the app (a loop's send) and the jobs use read_context too: they are not the voice turn."""
    put(db, KEY, turn("stranger"))
    assert read_context(db, "owner_chat").voice is None
    assert read_context(db, "owner_chat", model_turn=False).voice is None
    for origin in ("job", "voice_note"):
        assert read_context(db, origin, model_turn=True).voice is None


def test_the_flag_off_is_todays_behaviour(db):
    put(db, KEY, turn("stranger"))
    put(db, flags.PREFIX + FLAG, "0")
    assert read_context(db, "owner_chat", model_turn=True).voice is None
    put(db, flags.PREFIX + FLAG, "1")
    assert read_context(db, "owner_chat", model_turn=True).voice == "stranger"
    put(db, flags.PREFIX + FLAG, "garbage")                      # not 0 or 1: the default (on), like flags.on()
    assert read_context(db, "owner_chat", model_turn=True).voice == "stranger"


@pytest.mark.parametrize("value", ["", "not json", "[]", "null", "12", json.dumps({"label": "root"}), json.dumps({"label": 7}),
                                   json.dumps({"label": None}), json.dumps({"run": "x"}), json.dumps({"label": "Owner"}),
                                   json.dumps(["owner"])])
def test_a_record_that_cannot_be_read_raises(db, value):
    """Any error reading the label of a voice turn blocks (the hook turns a raise into exit 2)."""
    put(db, KEY, value)
    with pytest.raises(ValueError):
        read_context(db, "owner_chat", model_turn=True)
    assert read_context(db, "owner_chat").voice is None          # not a model call: not read, not a problem


def test_a_null_value_row_is_a_record_that_cannot_be_read(db):
    put(db, KEY, None)
    with pytest.raises(ValueError):
        read_context(db, "owner_chat", model_turn=True)


# ---------------------------------------------------------------- the hook itself (a real process, like the CLI starts it)

def hook(migrated, tool, args, **env):
    r = run_hook(json.dumps(event(P + tool, args)), hook_env(migrated, **env))
    return r.returncode, r.stderr


@pytest.mark.parametrize("label", BLOCKING)
def test_the_hook_blocks_a_change_by_a_voice_that_is_not_confirmed(migrated, db, label):
    put(db, KEY, turn(label))
    code, err = hook(migrated, "tasks_create", {"title": "x"})
    assert code == 2 and "Kairo gate" in err and "voice" in err.lower()
    for word in ("unsure", "stranger"):
        assert word not in err.lower()
    code, err = hook(migrated, "propose_action", {"action": "gmail.label", "args": {"message_id": "a", "add": ["Kairo/Reply"]}, "reason": "r"})
    assert code == 2 and "Kairo gate" in err


@pytest.mark.parametrize("label", BLOCKING)
def test_the_hook_lets_a_voice_that_is_not_confirmed_read(migrated, db, label):
    put(db, KEY, turn(label))
    assert hook(migrated, "tasks_list", {})[0] == 0
    assert hook(migrated, "gmail_search", {"query": "x"})[0] == 0


@pytest.mark.parametrize("label", ["owner", "unchecked"])
def test_the_hook_lets_an_owner_or_unchecked_voice_change_things(migrated, db, label):
    put(db, KEY, turn(label))
    assert hook(migrated, "tasks_create", {"title": "x"})[0] == 0
    ev = {"action": "gmail.reply", "args": {"message_id": "abc", "body": "hi"}, "reason": "r"}
    assert hook(migrated, "propose_action", ev)[0] == 0                       # a card, as always


def test_the_hook_with_no_voice_turn_is_what_it_was(migrated):
    assert hook(migrated, "tasks_create", {"title": "x"})[0] == 0


def test_the_hook_with_the_flag_off_is_what_it_was(migrated, db):
    put(db, KEY, turn("stranger"))
    put(db, flags.PREFIX + FLAG, "0")
    assert hook(migrated, "tasks_create", {"title": "x"})[0] == 0


def test_the_hook_ignores_the_label_for_a_job(migrated, db):
    """A job (nobody is talking) shares the database with a call that is going on: the call's label is not its."""
    put(db, KEY, turn("stranger"))
    assert hook(migrated, "tasks_create", {"title": "x"}, KAIRO_HOOK_ORIGIN="job")[0] == 0


@pytest.mark.parametrize("value", ["", "not json", "[]", json.dumps({"label": "root"}), json.dumps({"label": None})])
def test_the_hook_blocks_everything_when_the_label_cannot_be_read(migrated, db, value):
    put(db, KEY, value)
    for tool, args in (("tasks_create", {"title": "x"}), ("tasks_list", {}), ("gmail_search", {"query": "x"})):
        code, err = hook(migrated, tool, args)
        assert code == 2 and "failed closed" in err, (tool, err)


def test_the_hook_blocks_when_reading_the_label_fails(migrated, db, monkeypatch, capsys):
    """In-process, like test_hook's (a): whatever read_context raises on the label path is a block."""
    sys.path.insert(0, str(HOOK.parent))
    import io

    import pre_tool_use

    import app.gate_context as gc

    def boom(*a, **k):
        raise RuntimeError("the label table is gone")

    monkeypatch.setattr(gc, "_voice_label", boom)
    for k, v in hook_env(migrated).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event(P + "tasks_list", {}))))
    assert pre_tool_use.main() == 2
    assert "failed closed" in capsys.readouterr().err


def test_the_hook_asks_for_the_label_as_a_model_call(migrated, db, monkeypatch):
    """The hook is always a model's tool call: it must pass model_turn=True (read_context's default would not read the label)."""
    sys.path.insert(0, str(HOOK.parent))
    import io

    import pre_tool_use

    import app.gate_context as gc

    seen = []
    real = gc.read_context

    def spy(engine, origin, **kw):
        seen.append(kw)
        return real(engine, origin, **kw)

    monkeypatch.setattr(gc, "read_context", spy)
    for k, v in hook_env(migrated).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event(P + "tasks_list", {}))))
    assert pre_tool_use.main() == 0
    assert seen == [{"model_turn": True}]


def test_the_hook_file_still_exits_two_on_garbage(migrated):
    r = subprocess.run([sys.executable, str(HOOK)], input="{", env=hook_env(migrated), capture_output=True, text=True, timeout=60)
    assert r.returncode == 2
