"""The permission gate (KICKOFF 4, 8). Written before gate.py."""

from __future__ import annotations

import shutil

import pytest
import yaml

from app.gate import GateConfigError, GateContext, Gate, load_gate
from app.settings import REPO_ROOT

P = "mcp__kairo__"


@pytest.fixture(scope="module")
def gate() -> Gate:
    return load_gate(REPO_ROOT / "config")


def ctx(**kw) -> GateContext:
    base = dict(paused=False, origin="owner_chat", open_approvals=0, outbound_last_24h=0)
    base.update(kw)
    return GateContext(**base)


def propose(action, args, reason="because"):
    return P + "propose_action", {"action": action, "args": args, "reason": reason}


HOLD = {"summary": "Focus block", "start": "2026-10-20T14:00:00+02:00", "end": "2026-10-20T15:00:00+02:00"}


# ---------------------------------------------------------------- default deny

@pytest.mark.parametrize("tool", ["Bash", "Write", "Edit", "Read", "WebFetch", "PowerShell", "mcp__gmail__send_message",
                                  "mcp__other__anything", P + "does_not_exist", P + "gmail_send", ""])
def test_anything_not_listed_is_denied(gate, tool):
    d = gate.decide(tool, {}, ctx())
    assert d.verdict == "deny"


def test_unknown_proposed_action_is_denied(gate):
    d = gate.decide(*propose("gmail.forward_all", {"to": "x@example.com"}), ctx())
    assert d.verdict == "deny"


# ---------------------------------------------------------------- tier 0 / 1: direct tools

@pytest.mark.parametrize("tool,args,action,tier", [
    ("gmail_search", {"query": "from:school"}, "gmail.search", 0),
    ("gmail_read", {"message_id": "18c2a1b"}, "gmail.read", 0),
    ("calendar_list", {}, "calendar.list", 0),
    ("vault_read", {"path": "20 Goals/2026.md"}, "vault.read", 0),
    ("tasks_list", {}, "tasks.list", 0),
    ("tasks_create", {"title": "Revise thermodynamics"}, "tasks.create", 1),
    ("tasks_update", {"id": 3, "status": "done"}, "tasks.update", 1),
    ("notes_write", {"path": "brief.md", "content": "hi"}, "notes.write", 1),
])
def test_read_and_internal_tools_allowed(gate, tool, args, action, tier):
    d = gate.decide(P + tool, args, ctx())
    assert (d.verdict, d.action, d.tier) == ("allow", action, tier)


def test_direct_tool_with_unexpected_argument_is_denied(gate):
    assert gate.decide(P + "gmail_search", {"query": "x", "send_to": "y"}, ctx()).verdict == "deny"


def test_direct_tool_with_bad_argument_type_is_denied(gate):
    assert gate.decide(P + "tasks_update", {"id": "not-a-number"}, ctx()).verdict == "deny"


def test_low_tier_actions_cannot_be_proposed(gate):
    assert gate.decide(*propose("tasks.create", {"title": "x"}), ctx()).verdict == "deny"


# ---------------------------------------------------------------- tier 2: backend executes, no card

def test_label_is_executed_directly(gate):
    d = gate.decide(*propose("gmail.label", {"message_id": "abc", "add": ["Kairo/Reply"]}), ctx())
    assert (d.verdict, d.tier) == ("execute", 2)


def test_label_outside_kairo_namespace_is_denied(gate):
    d = gate.decide(*propose("gmail.label", {"message_id": "abc", "remove": ["INBOX"]}), ctx())
    assert d.verdict == "deny"


def test_draft_is_executed_directly(gate):
    d = gate.decide(*propose("gmail.create_draft", {"to": ["a@example.com"], "subject": "Hi", "body": "Text"}), ctx())
    assert d.verdict == "execute"


def test_hold_without_attendees_is_executed(gate):
    d = gate.decide(*propose("calendar.create_hold", HOLD), ctx())
    assert (d.verdict, d.action) == ("execute", "calendar.create_hold")


@pytest.mark.parametrize("extra", [
    {"attendees": [{"email": "x@example.com"}]},
    {"guests": ["x@example.com"]},
    {"sendUpdates": "all"},
    {"sendUpdates": "none"},
    {"sendNotifications": True},
    {"conferenceData": {"createRequest": {}}},
    {"guestsCanInviteOthers": True},
    {"guestsCanModify": True},
    {"visibility": "public"},          # not on the allow-list
    {"calendarId": "someone@example.com"},
])
def test_hold_that_could_notify_or_leave_own_calendar_is_denied(gate, extra):
    d = gate.decide(*propose("calendar.create_hold", {**HOLD, **extra}), ctx())
    assert d.verdict == "deny"
    assert d.reason


def test_hold_must_end_after_start(gate):
    d = gate.decide(*propose("calendar.create_hold", {**HOLD, "end": HOLD["start"]}), ctx())
    assert d.verdict == "deny"


# ---------------------------------------------------------------- tier 3 / 4: approval card

@pytest.mark.parametrize("action,args,tier", [
    ("gmail.send", {"to": ["a@example.com"], "subject": "Hi", "body": "x"}, 3),
    ("gmail.reply", {"message_id": "abc", "body": "Thanks!"}, 3),
    ("calendar.respond", {"event_id": "ev1", "response": "accepted"}, 3),
    ("gmail.trash", {"message_id": "abc"}, 4),
    ("calendar.delete", {"event_id": "ev1"}, 4),
])
def test_outbound_and_destructive_need_approval(gate, action, args, tier):
    d = gate.decide(*propose(action, args), ctx())
    assert (d.verdict, d.tier) == ("approve", tier)


def test_destructive_needs_second_confirmation(gate):
    assert gate.decide(*propose("gmail.trash", {"message_id": "abc"}), ctx()).confirm_twice
    assert not gate.decide(*propose("gmail.send", {"to": ["a@example.com"], "subject": "s", "body": "b"}), ctx()).confirm_twice


def test_invalid_recipient_is_denied(gate):
    d = gate.decide(*propose("gmail.send", {"to": ["not an address"], "subject": "s", "body": "b"}), ctx())
    assert d.verdict == "deny"


def test_jobs_cannot_create_cards(gate):
    """Jobs read untrusted mail with nobody watching: they may label and draft, never propose outbound."""
    d = gate.decide(*propose("gmail.reply", {"message_id": "abc", "body": "ok"}), ctx(origin="job"))
    assert d.verdict == "deny"
    assert gate.decide(*propose("gmail.label", {"message_id": "a", "add": ["Kairo/FYI"]}), ctx(origin="job")).verdict == "execute"


def test_open_approvals_limit(gate):
    d = gate.decide(*propose("gmail.trash", {"message_id": "abc"}), ctx(open_approvals=10))
    assert d.verdict == "deny"


def test_outbound_per_day_limit(gate):
    d = gate.decide(*propose("gmail.send", {"to": ["a@example.com"], "subject": "s", "body": "b"}), ctx(outbound_last_24h=20))
    assert d.verdict == "deny"


# ---------------------------------------------------------------- tier 5 and the kill switch

@pytest.mark.parametrize("action", ["payments.send", "bank.transfer", "twint.pay", "money.anything"])
def test_money_is_impossible(gate, action):
    assert gate.decide(*propose(action, {"amount": 10}), ctx()).verdict == "deny"


def test_kill_switch_denies_everything(gate):
    paused = ctx(paused=True)
    assert gate.decide(P + "gmail_search", {"query": "x"}, paused).verdict == "deny"
    assert gate.decide(*propose("gmail.label", {"message_id": "a", "add": ["Kairo/FYI"]}), paused).verdict == "deny"


def test_propose_action_rejects_unexpected_fields(gate):
    tool, args = propose("gmail.trash", {"message_id": "a"})
    args["approved"] = True
    assert gate.decide(tool, args, ctx()).verdict == "deny"


def test_decision_carries_normalised_args(gate):
    d = gate.decide(*propose("gmail.send", {"to": ["A@Example.com"], "subject": "s", "body": "b"}), ctx())
    assert d.args["to"] == ["A@Example.com"]
    assert d.args["cc"] == []


# ---------------------------------------------------------------- config can't weaken the rules

def write_config(tmp_path, mutate):
    shutil.copy(REPO_ROOT / "config" / "permissions.yaml", tmp_path / "permissions.yaml")
    data = yaml.safe_load((tmp_path / "permissions.yaml").read_text())
    mutate(data)
    (tmp_path / "permissions.yaml").write_text(yaml.safe_dump(data))
    return tmp_path


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(default="allow"),
    lambda d: d["tiers"]["3_outbound"].update(mode="auto"),
    lambda d: d["tiers"]["4_destructive"].update(mode="auto"),
    lambda d: d["tiers"]["5_money"].update(mode="approve"),
    lambda d: d["actions"].update({"bank.transfer": "5_money"}),
    lambda d: d["actions"].update({"gmail.send": "2_reversible"}),
    lambda d: d["actions"].update({"gmail.send": "nonexistent_tier"}),
    lambda d: d["rules"]["calendar.create_hold"]["allow_only_fields"].append("attendees"),
])
def test_config_that_weakens_the_gate_refuses_to_load(tmp_path, mutate):
    with pytest.raises(GateConfigError):
        load_gate(write_config(tmp_path, mutate))


def test_missing_or_broken_config_refuses_to_load(tmp_path):
    with pytest.raises(GateConfigError):
        load_gate(tmp_path)
    (tmp_path / "permissions.yaml").write_text("default: [unclosed")
    with pytest.raises(GateConfigError):
        load_gate(tmp_path)


def test_every_configured_action_has_an_argument_schema(gate):
    """'Never add an action without a test': an action without a schema can't be decided."""
    assert gate.actions_without_schema() == []
