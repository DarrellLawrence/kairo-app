"""Voice V1 (owner's request 2026-10-15): chat and calls run on ONE warm Claude process, Haiku by
default, Sonnet only when it's needed (writing in the owner's voice, judgment calls, several tool
steps), Opus for money. The system prompt is short and stays the same (so it's cached): personality,
a short profile card and the conversation memory. What changes per turn (the time, what the notes say
about this message) goes with the message. Written before app/conversation.py."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import conversation
from app.main import create_app


# ---------------------------------------------------------------- routing

@pytest.mark.parametrize("text, model", [
    ("what's on today?", "haiku"),
    ("add milk to the shopping list", "haiku"),
    ("wann ist mein Training?", "haiku"),
    ("draft a reply to Sam saying I can't make it", "sonnet"),
    ("write an email to the landlord about the heating", "sonnet"),
    ("schreib Kim eine Nachricht, dass ich später komme", "sonnet"),
    ("should I move the study block or skip training?", "sonnet"),
    ("soll ich heute lernen oder morgen?", "sonnet"),
    ("help me plan my week", "sonnet"),
])
def test_haiku_by_default_sonnet_when_needed(text, model):
    assert conversation.route(text) == model


def test_money_goes_to_opus_and_settings_can_raise_the_default():
    assert conversation.route("can I afford new shoes this month?", purpose="money") == "opus"
    assert conversation.route("what's on today?", schedule={"models": {"chat": "sonnet"}}) == "sonnet"


# Owner's rule 2026-10-19: calls run on the voice model + Haiku for ask_kairo, Sonnet only for a real judgment
# call, and every run that isn't Haiku says why. On the PC "call" ran Sonnet 9x: Kairo's own wording around the
# request ("Answer in one to three...", "the owner's answer to Kairo's...") matched "answer \w+".

def test_kairos_own_wording_around_a_call_request_stays_on_haiku():
    from app import realtime
    wrapped = [realtime.brain_prompt("what's on tomorrow?", ["Owner: what's on tomorrow?"]),
               "add milk\n[This is the owner's answer to Kairo's question L1. Record it as you would in chat.]"]
    for text in wrapped:
        assert conversation.route_why(text, purpose="call") == ("haiku", None), text[:60]
    for text in ("answer it", "Answer in one sentence", "what's the answer to this", "the answer I got was fine"):
        assert conversation.route(text, purpose="call") == "haiku", text
    # replying to a person is still writing in the owner's voice
    assert conversation.route_why("answer Sam that I'm late", purpose="call") == ("sonnet", '"answer Sam"')
    assert conversation.route("answer the email from the school") == "sonnet"


def test_a_request_with_a_raised_item_riding_along_is_routed_on_the_request_alone():
    from app import bringup
    item = bringup.Item("L1", "loop", "What do you think about the offer? (from a mail)")
    req = "add a reminder for the dentist\n\n" + bringup.report_request(item, "The offer?", "later", live=True)
    assert bringup.without_reports(req) == "add a reminder for the dentist"
    assert conversation.route(bringup.without_reports(req), purpose="call") == "haiku"
    assert conversation.route(req, purpose="call") == "sonnet"          # the item's text alone would have done it


def test_why_a_turn_is_not_on_haiku():
    assert conversation.route_why("should I skip training today?", purpose="call") == ("sonnet", '"should I"')
    assert conversation.route_why("soll ich heute lernen?") == ("sonnet", '"soll ich"')
    assert conversation.route_why("can I afford it?", purpose="money") == ("opus", "money")
    assert conversation.route_why("what's on?", purpose="call", schedule={"models": {"call": "opus"}}) == ("opus", "settings")
    assert conversation.route_why("add milk to the list", purpose="call") == ("haiku", None)
    assert conversation.route_why("should I?", purpose="learn_call") == ("haiku", None)
    assert conversation.handed_over(" judgment: which plan to drop\n") == "handed over by Haiku: judgment: which plan to drop"
    assert conversation.handed_over("") == "handed over by Haiku"


# ---------------------------------------------------------------- the escalation marker

def test_the_marker_never_reaches_the_owner():
    f = conversation.MarkerFilter()
    out = [f.feed(x) for x in ("[[son", "net]]")]
    assert out == ["", ""] and f.escalated()
    g = conversation.MarkerFilter()
    assert g.feed("[") == "" and g.feed("Note] done") == "[Note] done" and not g.escalated()
    h = conversation.MarkerFilter()
    assert h.feed("Sure, ") == "Sure, " and h.feed("[[sonnet]]") == "[[sonnet]]" and not h.escalated()   # only at the start


def test_haikus_reason_for_handing_over_is_kept_and_never_shown():
    f = conversation.MarkerFilter()
    out = [f.feed(x) for x in ("[[son", "net]] judg", "ment: which plan", " to drop\nand more")]
    assert out == ["", "", "", ""] and f.flush() == "" and f.escalated()
    assert f.why() == "judgment: which plan to drop"
    g = conversation.MarkerFilter()
    assert g.feed("[[sonnet]]") == "" and g.escalated() and g.why() == ""


# ---------------------------------------------------------------- the prompt

def test_the_header_carries_what_changes_per_turn():
    h = conversation.header(right_now="Right now: Wednesday 14:05.", hits="[70 Health/training.md] Boxing on Monday.",
                            extras="", fast=True)
    assert "Right now: Wednesday 14:05." in h and "Boxing on Monday" in h and conversation.ESCALATE in h
    assert conversation.ESCALATE not in conversation.header(right_now="Right now: x.", hits="", extras="", fast=False)


def test_on_a_call_haiku_keeps_the_tool_work():
    call = conversation.header(right_now="Right now: x.", hits="", extras="", fast=True, call=True)
    chat = conversation.header(right_now="Right now: x.", hits="", extras="", fast=True)
    assert conversation.FAST_CALL in call and "never a reason to hand over" in call and "real judgment call" in call
    assert "several steps with tools" not in call and "several steps with tools" in chat
    assert "a few words saying why" in call and "a few words saying why" in chat


def test_profile_card_is_short(tmp_path):
    (tmp_path / "10 Profile").mkdir(parents=True)
    (tmp_path / "10 Profile" / "me.md").write_text("# Me\n" + "A long profile line.\n" * 2000, encoding="utf-8")
    card = conversation.profile_card(tmp_path)
    assert len(card) <= conversation.CARD_LIMIT + 600 and "memory_search" in card      # head + cut note + pointer


# ---------------------------------------------------------------- end to end over the chat socket

def say(ws, text):
    ws.send_json({"type": "message", "text": text})
    deltas = []
    while True:
        f = ws.receive_json()
        if f["type"] == "delta":
            deltas.append(f["text"])
        if f["type"] in ("done", "error"):
            return f, "".join(deltas)


def test_chat_turns_share_one_warm_process_and_switch_models(migrated, monkeypatch, tmp_path):
    rec = tmp_path / "rec.json"
    monkeypatch.setenv("KAIRO_HOOK_FAKE_RECORD", str(rec))
    vault = migrated.vault_dir / "10 Profile"
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "me.md").write_text("# Me\n" + "Profile detail line.\n" * 2000, encoding="utf-8")
    with TestClient(create_app(migrated)) as c, c.websocket_connect("/ws/chat") as ws:
        a, _ = say(ws, "what's on today?")
        pid = json.loads(rec.read_text())["pid"]
        b, _ = say(ws, "draft a reply to Sam saying I'm late")
    data = json.loads(rec.read_text())
    assert data["pid"] == pid and len(data["turns"]) == 2            # one process, two turns
    assert data["models"] == ["haiku", "sonnet"] and a["model"] == "haiku" and b["model"] == "sonnet"
    assert "--input-format" in data["argv"]
    assert len(data["system"]) < 15000                                   # the whole profile is not in it
    assert "Right now:" not in data["system"]                           # it changes: it goes with the message
    assert "Right now:" in data["turns"][0] and data["turns"][0].rstrip().endswith("what's on today?")


def test_haiku_hands_over_to_sonnet_and_the_owner_never_sees_the_marker(migrated, monkeypatch, tmp_path):
    rec = tmp_path / "rec.json"
    monkeypatch.setenv("KAIRO_HOOK_FAKE_RECORD", str(rec))
    with TestClient(create_app(migrated)) as c, c.websocket_connect("/ws/chat") as ws:
        done, streamed = say(ws, "hmm, what about this [[fake-escalate]]")
        msgs = c.get("/api/chat/messages").json()["messages"]
    data = json.loads(rec.read_text())
    assert data["models"] == ["haiku", "sonnet"]
    assert done["model"] == "sonnet" and "[[sonnet]]" not in done["text"] and "[[sonnet]]" not in streamed
    assert "[[sonnet]]" not in msgs[-1]["text"] and msgs[-1]["role"] == "kairo"


def test_a_hand_over_says_why_and_more_usage_lists_it(migrated, monkeypatch, tmp_path):
    monkeypatch.setenv("KAIRO_HOOK_FAKE_RECORD", str(tmp_path / "rec.json"))
    with TestClient(create_app(migrated)) as c, c.websocket_connect("/ws/chat") as ws:
        ws.send_json({"type": "message", "text": "hmm, what about this [[fake-escalate: judgment, which plan to drop]]"})
        frames = []
        while (f := ws.receive_json())["type"] not in ("done", "error"):
            frames.append(f)
        b, streamed = say(ws, "should I move the study block or skip training?")
        u = c.get("/api/usage?days=1").json()
    model = next(x for x in frames if x["type"] == "model")
    assert model == {"type": "model", "model": "sonnet", "escalated": True,
                     "why": "handed over by Haiku: judgment, which plan to drop"}
    assert f["model"] == "sonnet" and f["server"]["why"] == model["why"] and "judgment" not in f["text"]
    assert b["model"] == "sonnet" and b["server"]["why"] == '"should I"'
    assert [(e["model"], e["purpose"], e["reason"]) for e in u["escalations"]] == [
        ("sonnet", "chat", '"should I"'), ("sonnet", "chat", "handed over by Haiku: judgment, which plan to drop")]
