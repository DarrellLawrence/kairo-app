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


@pytest.mark.parametrize("rule", [
    "RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20270131T235959Z",          # timetable: one school day, until semester end
    "RRULE:FREQ=WEEKLY;UNTIL=20270131",
])
def test_weekly_hold_with_an_end_is_executed(gate, rule):
    d = gate.decide(*propose("calendar.create_hold", {**HOLD, "recurrence": [rule], "time_zone": "Europe/Zurich"}), ctx())
    assert (d.verdict, d.action) == ("execute", "calendar.create_hold")


@pytest.mark.parametrize("extra", [
    {"recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO"], "time_zone": "Europe/Zurich"},                 # never ending
    {"recurrence": ["RRULE:FREQ=DAILY;UNTIL=20270131"], "time_zone": "Europe/Zurich"},           # weekly only
    {"recurrence": ["RRULE:FREQ=WEEKLY;COUNT=500"], "time_zone": "Europe/Zurich"},
    {"recurrence": ["RRULE:FREQ=WEEKLY;UNTIL=20270131", "RDATE:20270201"], "time_zone": "Europe/Zurich"},
    {"recurrence": ["RRULE:FREQ=WEEKLY;UNTIL=20270131;X-ATTENDEE=a@example.com"], "time_zone": "Europe/Zurich"},
    {"recurrence": ["RRULE:FREQ=WEEKLY;UNTIL=20290131"], "time_zone": "Europe/Zurich"},           # more than a year
    {"recurrence": ["RRULE:FREQ=WEEKLY;UNTIL=20270131"]},                                          # needs the zone
    {"recurrence": ["RRULE:FREQ=WEEKLY;UNTIL=20270131"], "time_zone": "Not/AZone"},
])
def test_recurring_hold_must_be_weekly_with_an_end_within_a_year(gate, extra):
    d = gate.decide(*propose("calendar.create_hold", {**HOLD, **extra}), ctx())
    assert d.verdict == "deny"


def test_hold_must_end_after_start(gate):
    d = gate.decide(*propose("calendar.create_hold", {**HOLD, "end": HOLD["start"]}), ctx())
    assert d.verdict == "deny"


# ---------------------------------------------------------------- tier 3 / 4: approval card

@pytest.mark.parametrize("action,args,tier", [
    ("gmail.send", {"to": ["a@example.com"], "subject": "Hi", "body": "x"}, 3),
    ("gmail.reply", {"message_id": "abc", "body": "Thanks!"}, 3),
    ("calendar.respond", {"event_id": "ev1", "response": "accepted"}, 3),
    ("calendar.delete", {"event_id": "ev1"}, 4),
])
def test_outbound_and_destructive_need_approval(gate, action, args, tier):
    d = gate.decide(*propose(action, args), ctx())
    assert (d.verdict, d.tier) == ("approve", tier)


def test_destructive_needs_second_confirmation(gate):
    assert gate.decide(*propose("calendar.delete", {"event_id": "ev1"}), ctx()).confirm_twice
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
    d = gate.decide(*propose("calendar.delete", {"event_id": "ev1"}), ctx(open_approvals=10))
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
    lambda d: d["actions"].update({"calendar.delete": "2_reversible"}),
    lambda d: d["actions"].update({"gmail.send_self": "1_internal"}),
    lambda d: d["actions"].update({"calendar.move": "1_internal"}),
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


# ---------------------------------------------------------------- phase 3: life data (written before the code)

@pytest.mark.parametrize("tool,args,tier", [
    ("shopping_list", {}, 0),
    ("shopping_add", {"items": ["Milch", "Brot"]}, 1),
    ("people_list", {"name": "Anna"}, 0),
    ("people_note", {"name": "Anna", "note": "Starts a new job in November", "contacted": True}, 1),
    ("finance_list", {"days": 30}, 0),
    ("health_list", {"days": 14}, 0),
])
def test_life_tools_are_direct_at_their_tier(gate, tool, args, tier):
    d = gate.decide(P + tool, args, ctx(origin="job"))
    assert (d.verdict, d.tier) == ("allow", tier)


@pytest.mark.parametrize("tool,args", [
    ("shopping_add", {"items": []}),
    ("shopping_add", {"items": ["x" * 201]}),
    ("shopping_add", {"items": ["ok"], "buy": True}),
    ("people_note", {"name": "", "note": "x"}),
    ("finance_list", {"days": 0}),
    ("finance_list", {"days": 30, "pay": "CH93 0076"}),
    ("health_list", {"days": 400}),
])
def test_life_tools_reject_bad_arguments(gate, tool, args):
    assert gate.decide(P + tool, args, ctx()).verdict == "deny"


# ---------------------------------------------------------------- v8 C6: memory search (written before the code)

def test_memory_search_is_a_read_tool(gate):
    d = gate.decide(P + "memory_search", {"query": "Wann ist mein Training?"}, ctx())
    assert (d.verdict, d.action, d.tier) == ("allow", "memory.search", 0)
    assert gate.decide(P + "memory_search", {"query": "x", "k": 8}, ctx(origin="job")).verdict == "allow"


@pytest.mark.parametrize("args", [
    {"query": ""}, {"query": "x" * 501}, {"query": "x", "k": 0}, {"query": "x", "k": 21},
    {"query": "x", "write": "10 Profile/x.md"}, {},
])
def test_memory_search_rejects_bad_arguments(gate, args):
    assert gate.decide(P + "memory_search", args, ctx()).verdict == "deny"


def test_memory_search_cannot_be_proposed(gate):
    assert gate.decide(*propose("memory.search", {"query": "x"}), ctx()).verdict == "deny"


@pytest.mark.parametrize("action", ["shopping.add", "people.note", "finance.list"])
def test_life_actions_cannot_be_proposed(gate, action):
    """Tier 0-1 are direct tools; proposing them is refused (and nothing about money exists)."""
    tool, args = propose(action, {})
    assert gate.decide(tool, args, ctx()).verdict == "deny"


def test_finance_is_read_only_and_money_still_impossible(gate):
    finance = {a: t for a, t in gate.actions.items() if a.startswith("finance.")}
    assert finance == {"finance.list": 0}
    for action in ("finance.pay", "finance.transfer", "twint.send"):
        tool, args = propose(action, {"amount": 10})
        assert gate.decide(tool, args, ctx()).verdict == "deny"


def test_voice_notes_act_but_cannot_create_cards(gate):
    """A Shortcut voice note runs with nobody watching (KICKOFF v7 item 7): own-only actions run,
    anything that would reach another person waits for the owner in the app."""
    d = gate.decide(*propose("gmail.reply", {"message_id": "abc", "body": "ok"}), ctx(origin="voice_note"))
    assert d.verdict == "deny"
    assert gate.decide(*propose("gmail.label", {"message_id": "a", "add": ["Kairo/FYI"]}), ctx(origin="voice_note")).verdict == "execute"


# ---------------------------------------------------------------- v7: act alone on what only touches the owner (before the code)

def test_trash_is_reversible_now(gate):
    """Gmail keeps trash 30 days: tier 2, runs at once, in the digest."""
    d = gate.decide(*propose("gmail.trash", {"message_id": "abc"}), ctx(origin="job"))
    assert (d.verdict, d.tier) == ("execute", 2)


def test_mail_to_self_runs_and_has_no_recipient_field(gate):
    ok = {"subject": "Study sheet: Thermo", "body": "Notes", "label": "Kairo/School"}
    d = gate.decide(*propose("gmail.send_self", ok), ctx(origin="job"))
    assert (d.verdict, d.tier) == ("execute", 2)
    for extra in ({"to": ["x@example.com"]}, {"cc": ["x@example.com"]}, {"bcc": ["x@example.com"]}):
        assert gate.decide(*propose("gmail.send_self", {**ok, **extra}), ctx()).verdict == "deny"
    assert gate.decide(*propose("gmail.send_self", {**ok, "label": "INBOX"}), ctx()).verdict == "deny"
    assert gate.decide(*propose("gmail.send_self", {**ok, "subject": "a\nBcc: x@example.com"}), ctx()).verdict == "deny"


@pytest.mark.parametrize("kind", ["FIXED", "SOCIAL", "FLEXIBLE"])
def test_tagging_an_event_is_internal(gate, kind):
    d = gate.decide(P + "calendar_tag", {"event_id": "ev1", "type": kind}, ctx())
    assert (d.verdict, d.tier) == ("allow", 1)


def test_bad_tag_is_denied(gate):
    assert gate.decide(P + "calendar_tag", {"event_id": "ev1", "type": "WHATEVER"}, ctx()).verdict == "deny"


MOVE = {"event_id": "ev1", "start": "2026-10-20T16:00:00+02:00", "end": "2026-10-20T17:00:00+02:00"}


def test_moving_an_own_event_is_reversible(gate):
    d = gate.decide(*propose("calendar.move", MOVE), ctx(origin="voice_note"))
    assert (d.verdict, d.tier) == ("execute", 2)


@pytest.mark.parametrize("extra", [{"attendees": []}, {"sendUpdates": "all"}, {"summary": "x"}, {"end": MOVE["start"]}])
def test_move_allows_only_new_times(gate, extra):
    assert gate.decide(*propose("calendar.move", {**MOVE, **extra}), ctx()).verdict == "deny"


# ---------------------------------------------------------------- open loops (written before the code)

@pytest.mark.parametrize("tool,args,tier", [
    ("loops_list", {}, 0),
    ("loops_add", {"kind": "promise", "who": "Lea", "what": "send her the ABU notes", "due": "2026-10-15"}, 1),
    ("loops_snooze", {"id": 3, "when": "later"}, 1),
    ("loops_snooze", {"id": 3, "when": "in_1h"}, 1),
    ("loops_snooze", {"id": 3, "when": "tomorrow"}, 1),
    ("loops_snooze", {"id": 3, "at": "2026-10-15T16:00:00+02:00"}, 1),
    ("loops_close", {"id": 3, "reason": "dropped"}, 1),
    ("loops_close", {"id": 3, "reason": "done"}, 1),
    ("loops_close", {"id": 3, "reason": "not_for_me"}, 1),        # "not a real reply": mutes that sender's mails
])
def test_loop_tools_are_internal(gate, tool, args, tier):
    d = gate.decide(P + tool, args, ctx(origin="voice_note"))
    assert (d.verdict, d.tier) == ("allow", tier)


@pytest.mark.parametrize("tool,args", [
    ("loops_snooze", {"id": 3, "when": "never"}),
    ("loops_snooze", {"id": 3}),                                   # when or at
    ("loops_snooze", {"id": 3, "at": "2026-10-15T16:00:00"}),      # needs a time zone
    ("loops_close", {"id": 3, "reason": "sent"}),                  # sending is never a loop tool
    ("loops_close", {"id": 3, "reason": "automated"}),             # only the backend decides that
    ("loops_add", {"kind": "promise", "who": "Lea", "what": ""}),
    ("loops_add", {"kind": "send", "who": "Lea", "what": "x"}),
])
def test_bad_loop_calls_are_denied(gate, tool, args):
    assert gate.decide(P + tool, args, ctx()).verdict == "deny"


@pytest.mark.parametrize("args", [{}, {"scope": "everything"}, {"scope": "auto", "topic": "school"}, {"scope": "week"}])
def test_debrief_is_a_read_tool(gate, args):
    d = gate.decide(P + "debrief", args, ctx(origin="voice_note"))
    assert (d.verdict, d.tier) == ("allow", 0)


def test_debrief_rejects_unknown_topics(gate):
    assert gate.decide(P + "debrief", {"topic": "bank_transfer"}, ctx()).verdict == "deny"


def test_people_note_can_store_an_email_but_only_a_valid_one(gate):
    ok = gate.decide(P + "people_note", {"name": "Marc", "email": "marc@example.com"}, ctx())
    assert (ok.verdict, ok.tier) == ("allow", 1)
    assert gate.decide(P + "people_note", {"name": "Marc", "email": "not an address"}, ctx()).verdict == "deny"


# ---------------------------------------------------------------- settings by voice (written before the code)

@pytest.mark.parametrize("tool,args,tier", [
    ("settings_list", {}, 0),
    ("settings_set", {"key": "quiet_hours", "value": {"start": "23:00", "end": "06:30"}}, 1),
    ("settings_set", {"key": "max_pings", "value": 8}, 1),
    ("place_set", {"name": "Home", "kind": "home", "station": "Hometown"}, 1),
    ("place_set", {"name": "Work 2", "kind": "work", "station": "Worktown", "valid_from": "2027-08-01"}, 1),
    ("place_remove", {"name": "Home"}, 1),
])
def test_settings_tools_are_internal(gate, tool, args, tier):
    d = gate.decide(P + tool, args, ctx(origin="voice_note"))
    assert (d.verdict, d.tier) == ("allow", tier)


@pytest.mark.parametrize("tool,args", [
    ("settings_set", {"key": "gate", "value": "off"}),                    # only listed settings exist
    ("settings_set", {"key": "permissions", "value": {}}),
    ("place_set", {"name": "Home", "kind": "castle"}),
    ("place_set", {"name": "", "kind": "home"}),
])
def test_unknown_settings_are_denied(gate, tool, args):
    assert gate.decide(P + tool, args, ctx()).verdict == "deny"


# ---------------------------------------------------------------- v8 block B: quick questions + wishlists (written before the code)

@pytest.mark.parametrize("tool,args", [
    ("captures_answer", {"capture_id": 12, "answer": "Yes, from my girlfriend. Gift idea."}),
    ("wishlist_add", {"person": "Anna", "title": "Leather bag", "price_chf": 25.0, "url": "https://www.vinted.ch/items/1"}),
    ("wishlist_add", {"person": "Anna", "title": "Board game"}),
])
def test_quick_answers_and_wishlists_are_internal(gate, tool, args):
    d = gate.decide(P + tool, args, ctx())
    assert (d.verdict, d.tier) == ("allow", 1)


@pytest.mark.parametrize("tool,args", [
    ("captures_answer", {"capture_id": 0, "answer": "x"}),
    ("captures_answer", {"capture_id": 3, "answer": ""}),
    ("captures_answer", {"capture_id": 3, "answer": "x", "approve": True}),
    ("wishlist_add", {"person": "", "title": "x"}),
    ("wishlist_add", {"person": "Anna", "title": "x", "url": "javascript:alert(1)"}),
    ("wishlist_add", {"person": "Anna", "title": "x", "price_chf": -5}),
    ("wishlist_add", {"person": "Anna", "title": "x", "buy": True}),
])
def test_quick_answers_and_wishlists_reject_bad_arguments(gate, tool, args):
    assert gate.decide(P + tool, args, ctx()).verdict == "deny"


@pytest.mark.parametrize("action", ["captures.answer", "wishlist.add"])
def test_quick_answers_and_wishlists_cannot_be_proposed(gate, action):
    tool, args = propose(action, {})
    assert gate.decide(tool, args, ctx()).verdict == "deny"
