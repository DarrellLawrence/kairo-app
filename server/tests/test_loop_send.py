"""Loop Send (fix P1.6/7, diagnosis D5.3, D1.8, D1.9). Written before the code.

- Only to verified addresses: the real Reply-To/From of the mail being answered, a real To/Cc of
  the sent mail a promise came from, or an address the owner set or confirmed in the app. An
  address the model wrote (promise finder, people_note) is never used on its own.
- The owner sees the full address before sending (preview), and the send names it.
- The card goes through the same path as every card, actor = owner; tier 4 needs the confirm
  step first; 10 open cards don't block it; no card is ever left open.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlmodel import Session, select

from app import approvals, life, loops
from app.db import Approval, AuditLog, OpenLoop
from tests.fakes.fake_google import FakeGoogle, connect, decode_raw
from tests.test_loops import DRAFT, PROFILE, TIMES, TZ, client, env, triaged  # noqa: F401  (fixtures)


def add_loop(engine, **kw) -> int:
    base = dict(kind="promise", source="promise", source_ref="s1:send the ABU notes", what="send the ABU notes",
                who="Lea", since=dt.datetime.now(dt.timezone.utc), urgency="normal", status="open",
                draft_json=json.dumps({"text": "Here are the notes!", "subject": "ABU notes"}))
    base.update(kw)
    with Session(engine) as s:
        loop = OpenLoop(**base)
        s.add(loop)
        s.commit()
        s.refresh(loop)
        return loop.id


def preview(client, lid, **body):
    return client.post(f"/api/loops/{lid}/send", json={"text": "Here are the notes!", "preview": True, **body})


def sent_to(env) -> list[str]:
    return [decode_raw(m["raw"])["To"] for m in env["fake"].sent]


# ---------------------------------------------------------------- where it may go

def test_reply_goes_to_the_real_sender_and_the_preview_shows_it(client, env):
    with Session(env["engine"]) as s:
        triaged(s)
        loops.sync(s, now=dt.datetime.now(TZ), tz=TZ, times=TIMES)
        loop = loops.open_loops(s)[0]
        loop.who_email = "attacker@evil.example"          # whatever got stored: the header decides
        loop.draft_json = json.dumps(DRAFT)
        s.add(loop)
        s.commit()
        lid = loop.id
    p = preview(client, lid).json()
    assert p["to"] == "anna@example.com" and p["verified"] is True and p["action"] == "gmail.reply"
    assert env["fake"].sent == []                           # a preview sends nothing, makes no card
    with Session(env["engine"]) as s:
        assert s.exec(select(Approval)).all() == []
    r = client.post(f"/api/loops/{lid}/send", json={"text": "Thursday works!", "to": "anna@example.com"}).json()
    assert r["card"]["status"] == "done" and sent_to(env) == ["anna@example.com"]


def test_promise_uses_the_real_recipient_of_the_sent_mail(client, env):
    env["fake"].add_message("s1", sender="Me <owner@example.com>", subject="Notes", body="I'll send the notes.",
                            to="Lea Muster <lea@example.com>")
    lid = add_loop(env["engine"], who_email="lea@evil.example")    # what the model claimed
    p = preview(client, lid).json()
    assert p["to"] == "lea@example.com" and p["verified"] is True
    r = client.post(f"/api/loops/{lid}/send", json={"text": "Here are the notes!", "to": "lea@example.com"}).json()
    assert r["card"]["status"] == "done" and sent_to(env) == ["lea@example.com"]


def test_promise_with_several_recipients_takes_the_one_the_model_named_only_if_it_is_real(client, env):
    env["fake"].add_message("s1", sender="Me <owner@example.com>", subject="Notes", body="x",
                            to="Lea <lea@example.com>, Tom <tom@example.com>", cc="boss@example.com")
    lid = add_loop(env["engine"], who_email="tom@example.com")
    assert preview(client, lid).json()["to"] == "tom@example.com"
    lid2 = add_loop(env["engine"], source_ref="s1:other", who_email="x@evil.example")
    p = preview(client, lid2).json()                          # loops block L: the owner picks, never "send it yourself"
    assert [x["email"] for x in p["pick"]] == ["lea@example.com", "tom@example.com", "boss@example.com"]


def test_an_address_the_model_wrote_needs_the_owner_once(client, env):
    with Session(env["engine"]) as s:
        life.people_note(s, name="Lea", email="lea@example.com", today=dt.date.today())   # the model's tool
    lid = add_loop(env["engine"], kind="birthday", source="birthday", source_ref="1:2026-10-20:msg", who_email="lea@example.com")
    p = preview(client, lid).json()
    assert p["to"] == "lea@example.com" and p["verified"] is False
    r = client.post(f"/api/loops/{lid}/send", json={"text": "Happy birthday!", "to": "lea@example.com"})
    assert r.status_code == 409 and "confirm" in r.json()["detail"]
    assert env["fake"].sent == []
    r = client.post(f"/api/loops/{lid}/send", json={"text": "Happy birthday!", "to": "lea@example.com",
                                                    "confirm_address": "lea@example.com"}).json()
    assert r["card"]["status"] == "done" and sent_to(env) == ["lea@example.com"]
    lid2 = add_loop(env["engine"], kind="people", source="people", source_ref="1:catchup", who_email="lea@example.com")
    assert preview(client, lid2).json()["verified"] is True        # confirmed once is enough


def test_an_address_the_owner_typed_is_verified(client, env):
    client.post("/api/people", json={"name": "Tom", "email": "Tom@Example.com"})
    lid = add_loop(env["engine"], kind="people", source="people", source_ref="2:catchup", who="Tom", who_email="tom@example.com")
    assert preview(client, lid).json()["verified"] is True
    people = client.get("/api/people").json()["people"]
    assert [(p["email"], p["email_verified"]) for p in people if p["name"] == "Tom"] == [("tom@example.com", True)]


def test_the_address_sent_must_be_the_one_shown(client, env):
    env["fake"].add_message("s1", sender="Me <owner@example.com>", subject="Notes", body="x", to="lea@example.com")
    lid = add_loop(env["engine"])
    r = client.post(f"/api/loops/{lid}/send", json={"text": "hi", "to": "someone@else.example"})
    assert r.status_code == 409 and env["fake"].sent == []
    assert client.post(f"/api/loops/{lid}/send", json={"text": "hi"}).status_code == 409   # no address seen, no send


# ---------------------------------------------------------------- same path as every card, never stranded

def test_card_actor_is_the_owner(client, env):
    env["fake"].add_message("s1", sender="Me <owner@example.com>", subject="Notes", body="x", to="lea@example.com")
    lid = add_loop(env["engine"])
    client.post(f"/api/loops/{lid}/send", json={"text": "hi", "to": "lea@example.com"})
    with Session(env["engine"]) as s:
        created = [a for a in s.exec(select(AuditLog)).all() if "created" in (a.outcome or "")]
        assert [a.actor for a in created] == ["owner"]


def test_ten_open_cards_do_not_block_a_send(client, env):
    env["fake"].add_message("s1", sender="Me <owner@example.com>", subject="Notes", body="x", to="lea@example.com")
    with Session(env["engine"]) as s:
        for i in range(10):
            s.add(Approval(tool="gmail.send", args_json="{}", summary=f"c{i}", tier=3, status="open"))
        s.commit()
    lid = add_loop(env["engine"])
    r = client.post(f"/api/loops/{lid}/send", json={"text": "hi", "to": "lea@example.com"}).json()
    assert r["card"]["status"] == "done"


def test_tier_4_needs_the_confirm_step_and_leaves_no_card(client, env, monkeypatch):
    env["fake"].add_message("s1", sender="Me <owner@example.com>", subject="Notes", body="x", to="lea@example.com")
    gate = client.app_.state.gate
    monkeypatch.setitem(gate.actions, "gmail.send", 4)
    lid = add_loop(env["engine"])
    assert preview(client, lid).json()["confirm_twice"] is True
    r = client.post(f"/api/loops/{lid}/send", json={"text": "hi", "to": "lea@example.com"})
    assert r.status_code == 409 and "confirm" in r.json()["detail"]
    with Session(env["engine"]) as s:
        assert s.exec(select(Approval)).all() == []                   # nothing stranded
    r = client.post(f"/api/loops/{lid}/send", json={"text": "hi", "to": "lea@example.com", "confirm": True}).json()
    assert r["card"]["status"] == "done" and r["card"]["tier"] == 4


def test_a_send_that_cannot_go_out_leaves_no_open_card(client, env, monkeypatch):
    env["fake"].add_message("s1", sender="Me <owner@example.com>", subject="Notes", body="x", to="lea@example.com")
    lid = add_loop(env["engine"])
    monkeypatch.setattr(approvals, "_outbound_last_24h", lambda s: 10_000)
    r = client.post(f"/api/loops/{lid}/send", json={"text": "hi", "to": "lea@example.com"})
    assert r.status_code == 409 and "limit" in r.json()["detail"]
    with Session(env["engine"]) as s:
        assert [c.status for c in s.exec(select(Approval)).all() if c.status == "open"] == []
    assert env["fake"].sent == []
