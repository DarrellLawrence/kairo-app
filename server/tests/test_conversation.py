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


# ---------------------------------------------------------------- the escalation marker

def test_the_marker_never_reaches_the_owner():
    f = conversation.MarkerFilter()
    out = [f.feed(x) for x in ("[[son", "net]]")]
    assert out == ["", ""] and f.escalated()
    g = conversation.MarkerFilter()
    assert g.feed("[") == "" and g.feed("Note] done") == "[Note] done" and not g.escalated()
    h = conversation.MarkerFilter()
    assert h.feed("Sure, ") == "Sure, " and h.feed("[[sonnet]]") == "[[sonnet]]" and not h.escalated()   # only at the start


# ---------------------------------------------------------------- the prompt

def test_the_header_carries_what_changes_per_turn():
    h = conversation.header(right_now="Right now: Wednesday 14:05.", hits="[70 Health/training.md] Boxing on Monday.",
                            extras="", fast=True)
    assert "Right now: Wednesday 14:05." in h and "Boxing on Monday" in h and conversation.ESCALATE in h
    assert conversation.ESCALATE not in conversation.header(right_now="Right now: x.", hits="", extras="", fast=False)


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
