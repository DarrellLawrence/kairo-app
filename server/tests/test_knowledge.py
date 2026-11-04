"""Kairo calls to get to know the owner (owner's request 2026-10-14).

Collects what it doesn't know, calls at a good moment (never during school/work/training,
max 1 call a day, declined = another day), 3-4 topics, smart follow-ups and contradiction
checks by the model; everything learned goes to the vault; one line after the call."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import knowledge, life
from app.db import Call, KnowledgeItem, Person, make_engine
from app.main import create_app
from app.profile import load_profile
from app.settings import REPO_ROOT

PROFILE = load_profile(REPO_ROOT / "config")
TZ = PROFILE.tz
TIMES = {"after_work": "17:30", "weekend": "10:00"}
WED_EVENING = dt.datetime(2026, 10, 14, 19, 30, tzinfo=TZ)   # in the "evening at home" call window
WED_MORNING = dt.datetime(2026, 10, 14, 10, 0, tzinfo=TZ)
SAT = dt.datetime(2026, 10, 17, 11, 0, tzinfo=TZ)


@pytest.fixture
def engine(migrated):
    e = make_engine(migrated.db_url)
    yield e
    e.dispose()


# ---------------------------------------------------------------- collecting questions

def test_questions_are_queued_once_and_never_ask_for_secrets(engine):
    with Session(engine) as s:
        knowledge.ask_later(s, "Who is Alex to you?", about="Alex")
        knowledge.ask_later(s, "who is alex to you?", about="Alex")              # same question: once
        for bad in ("What's your OLAT password?", "Can you tell me your bank PIN?", "What's the 2FA code you got?",
                    "What is your IBAN?", "Read me your credit card number"):
            with pytest.raises(knowledge.KnowledgeError):
                knowledge.ask_later(s, bad)
        assert [q.question for q in knowledge.open_questions(s)] == ["Who is Alex to you?"]


def test_rules_find_gaps_about_people(engine):
    with Session(engine) as s:
        s.add(Person(name="Alex"))                                        # no relation
        s.add(Person(name="Mum", relation="mother"))                          # family, no birthday
        s.add(Person(name="Kim", relation="classmate"))                       # not close: no birthday needed
        s.commit()
        knowledge.find_gaps(s)
        knowledge.find_gaps(s)                                                # twice: no duplicates
        qs = sorted(q.question for q in knowledge.open_questions(s))
        assert qs == ["When is Mum's birthday?", "Who is Alex to you?"]


def test_no_family_known_is_a_question(engine):
    with Session(engine) as s:
        s.add(Person(name="Kim", relation="classmate"))
        s.commit()
        knowledge.find_gaps(s)
        assert "Who's in your close family?" in [q.question for q in knowledge.open_questions(s)]


# ---------------------------------------------------------------- when Kairo calls

def test_good_moment():
    free = lambda now: False   # noqa: E731
    busy = lambda now: True    # noqa: E731
    assert knowledge.good_moment(WED_EVENING, TZ, TIMES, busy=free, place_kind=None)[0]
    assert not knowledge.good_moment(WED_MORNING, TZ, TIMES, busy=free, place_kind=None)[0]          # school/work hours
    assert not knowledge.good_moment(WED_EVENING, TZ, TIMES, busy=busy, place_kind=None)[0]          # in an event
    assert not knowledge.good_moment(WED_EVENING, TZ, TIMES, busy=free, place_kind="training")[0]    # at training
    assert not knowledge.good_moment(WED_EVENING.replace(hour=21, minute=30), TZ, TIMES, busy=free, place_kind=None)[0]
    assert knowledge.good_moment(SAT, TZ, TIMES, busy=free, place_kind="home")[0]


class Pushes:
    def __init__(self):
        self.sent = []

    def notify(self, title, body, url="/", **kw):
        self.sent.append((title, body, url))


def caller(engine, pushes, busy=False, place=None):
    return knowledge.Caller(engine=engine, tz=TZ, times=TIMES, pusher=pushes,
                            busy=lambda now: busy, place_kind=lambda: place)


def seed(s, n=6):
    for i in range(n):
        knowledge.ask_later(s, f"Question number {i}?", about=f"topic{i}")


def test_calls_once_a_day_with_three_or_four_topics(engine):
    pushes = Pushes()
    c = caller(engine, pushes)
    with Session(engine) as s:
        seed(s)
    out = c.run(WED_EVENING)
    assert out["called"]
    assert pushes.sent[0][0] == "Kairo is calling" and pushes.sent[0][2].startswith("/#call/")
    with Session(engine) as s:
        call = s.exec(select(Call)).one()
        assert call.purpose == "learn" and len(json.loads(call.agenda_json)) == 4
        assert "get to know you" in call.opening and "Question number 0?" in call.opening
    assert not c.run(WED_EVENING + dt.timedelta(hours=1))["called"]                # max 1 a day
    with Session(engine) as s:                                                       # missed/declined: another day
        call = s.exec(select(Call)).one()
        call.status = "missed"
        s.add(call)
        s.commit()
    assert not c.run(WED_EVENING + dt.timedelta(hours=2))["called"]
    assert not c.run(WED_EVENING + dt.timedelta(days=1))["called"]                  # one missed: 2 days' rest
    assert c.run(WED_EVENING + dt.timedelta(days=2))["called"]


def test_a_missed_call_counts_as_asked_and_three_missed_drop_the_questions(engine):
    """Owner 2026-11-04: missed getting-to-know-you calls piled up; the same questions rang again every day."""
    pushes = Pushes()
    c = caller(engine, pushes)
    with Session(engine) as s:
        seed(s, 4)
    day = WED_EVENING
    for _ in range(3):
        assert c.run(day)["called"]
        day += dt.timedelta(days=16)                      # past ringing (missed) and past any rest
    assert c.run(day)["called"] is False                  # all four asked on 3 missed calls: dropped, nothing to ask
    with Session(engine) as s:
        assert {q.status for q in s.exec(select(KnowledgeItem)).all()} == {"dropped"}
        assert all(call.status == "missed" and call.ended_at for call in s.exec(select(Call)).all())
        assert {q.asks for q in s.exec(select(KnowledgeItem)).all()} == {3}     # each missed call counted once


def test_missed_calls_in_a_row_wait_longer(engine):
    c = caller(engine, Pushes())
    with Session(engine) as s:
        seed(s, 8)
    assert c.run(WED_EVENING)["called"]
    out = c.run(WED_EVENING + dt.timedelta(days=1))       # the first one rang out: missed
    assert not out["called"] and "1 missed" in out["why"]
    assert c.run(WED_EVENING + dt.timedelta(days=2))["called"]
    out = c.run(WED_EVENING + dt.timedelta(days=5))       # two in a row: 4 days
    assert not out["called"] and "2 missed" in out["why"]
    assert c.run(WED_EVENING + dt.timedelta(days=6))["called"]


def test_not_enough_to_ask_waits(engine):
    with Session(engine) as s:
        seed(s, 2)
    assert not caller(engine, Pushes()).run(WED_EVENING)["called"]
    with Session(engine) as s:                                                     # but not forever: 3 days old
        for q in s.exec(select(KnowledgeItem)).all():
            q.created_at = (WED_EVENING - dt.timedelta(days=4)).astimezone(dt.timezone.utc)   # the test clock, not the real one
            s.add(q)
        s.commit()
    assert caller(engine, Pushes()).run(WED_EVENING)["called"]


def test_no_call_at_a_bad_moment(engine):
    with Session(engine) as s:
        seed(s)
    assert not caller(engine, Pushes(), busy=True).run(WED_EVENING)["called"]
    assert not caller(engine, Pushes(), place="work").run(WED_EVENING)["called"]
    assert not caller(engine, Pushes()).run(WED_MORNING)["called"]


# ---------------------------------------------------------------- during and after the call

def test_agenda_for_the_model_has_what_is_known(engine):
    with Session(engine) as s:
        s.add(Person(name="Sam", relation="friend"))
        s.add(Person(name="Alex"))
        s.commit()
        knowledge.ask_later(s, "Who is Alex to you?", about="Alex")
        knowledge.ask_later(s, "Who's in your close family?", about="family")
        knowledge.ask_later(s, "Which class are you in?", about="school")
    caller(engine, Pushes()).run(WED_EVENING)
    with Session(engine) as s:
        call = s.exec(select(Call)).one()
        text = knowledge.agenda_prompt(s, call)
    assert "Who is Alex to you?" in text and "Sam (friend)" in text
    assert "follow-up" in text and "contradict" in text and "stop" in text.lower()
    assert "knowledge_answered" in text and "people_note" in text and "profile_learn" in text


def test_after_the_call_one_line_and_unanswered_come_back(engine, tmp_path):
    pushes = Pushes()
    with Session(engine) as s:
        knowledge.ask_later(s, "Who is Alex to you?", about="Alex")
        knowledge.ask_later(s, "When is Mum's birthday?", about="Mum")
        knowledge.ask_later(s, "Which class are you in?", about="school")
        knowledge.ask_later(s, "What do you do on weekends?", about="weekends")
    caller(engine, pushes).run(WED_EVENING)
    with Session(engine) as s:
        call = s.exec(select(Call)).one()
        call.status, call.answered_at = "answered", dt.datetime.now(dt.timezone.utc)
        s.add(call)
        s.commit()
        cat = s.exec(select(KnowledgeItem).where(KnowledgeItem.about == "Alex")).one()
        knowledge.answered(s, cat.id, "Alex: partner, birthday 9 March")
        knowledge.learn_fact(s, tmp_path / "vault", "Spends weekends cycling or at the gym")
        line = knowledge.finish(s, call)
    assert line == "Saved from our call: Alex: partner, birthday 9 March; Spends weekends cycling or at the gym."
    with Session(engine) as s:
        left = {q.about: (q.status, q.asks) for q in s.exec(select(KnowledgeItem).where(KnowledgeItem.kind == "question")).all()}
    assert left["Alex"][0] == "answered" and left["Mum"] == ("open", 1)
    profile_md = (tmp_path / "vault" / "10 Profile" / "Learned by Kairo.md").read_text(encoding="utf-8")
    assert "Spends weekends cycling or at the gym" in profile_md


def test_asked_three_times_is_dropped(engine):
    with Session(engine) as s:
        q = knowledge.ask_later(s, "What's your favourite food?", about="food")
        for _ in range(3):
            call = Call(reason="x", opening="x", purpose="learn", agenda_json=json.dumps([q.id]), status="ended")
            s.add(call)
            s.commit()
            knowledge.finish(s, call)
        assert s.get(KnowledgeItem, q.id).status == "dropped"


def test_people_go_to_the_vault_with_birthday(engine, tmp_path):
    with Session(engine) as s:
        p = life.people_note(s, name="Alex", relation="partner", birthday=dt.date(2001, 3, 9), note="anniversary 20 August",
                             today=dt.date(2026, 10, 14), vault_dir=tmp_path / "vault")
        assert p.birthday == dt.date(2001, 3, 9)
    md = (tmp_path / "vault" / "60 People" / "Alex.md").read_text(encoding="utf-8")
    assert "partner" in md and "9 March" in md and "anniversary 20 August" in md


# ---------------------------------------------------------------- through the app

@pytest.fixture
def client(migrated):
    app = create_app(migrated)
    app.state.pusher.notify = lambda *a, **kw: None
    with TestClient(app) as c:
        c.app_ = app
        yield c


def test_hang_up_sends_the_one_line(client, migrated):
    sent = []
    client.app_.state.pusher.notify = lambda title, body, url="/", **kw: sent.append(body)
    engine = make_engine(migrated.db_url)
    with Session(engine) as s:
        q = knowledge.ask_later(s, "Who is Alex to you?", about="Alex")
        call = Call(reason="Getting to know you", opening="Hi", purpose="learn", agenda_json=json.dumps([q.id]))
        s.add(call)
        s.commit()
        cid, qid = call.id, q.id
    client.post(f"/api/voice/calls/{cid}/answer", json={})
    with Session(engine) as s:
        knowledge.answered(s, qid, "Alex: partner")
    client.post(f"/api/voice/calls/{cid}/end", json={})
    engine.dispose()
    assert sent == ["Saved from our call: Alex: partner."]


def test_agenda_reaches_the_model_only_on_an_answered_learning_call(migrated, tmp_path, monkeypatch):
    rec = tmp_path / "rec.json"
    monkeypatch.setenv("KAIRO_HOOK_FAKE_RECORD", str(rec))
    engine = make_engine(migrated.db_url)
    with Session(engine) as s:
        q = knowledge.ask_later(s, "Who is Alex to you?", about="Alex")
        call = Call(reason="Getting to know you", opening="Hi", purpose="learn", agenda_json=json.dumps([q.id]))
        s.add(call)
        s.commit()
        cid = call.id
    engine.dispose()
    with TestClient(create_app(migrated)) as client, client.websocket_connect("/ws/chat") as ws:
        def say(text):
            ws.send_json({"type": "message", "text": text, "call_id": cid})
            while True:
                f = ws.receive_json()
                if f["type"] in ("done", "error"):
                    return f
        assert "Kairo's notes" not in say("(spoken, on a call) hi")["text"]          # still ringing: no agenda
        client.post(f"/api/voice/calls/{cid}/answer", json={})
        reply = say("(spoken, on a call) they're my partner")["text"]
        assert "#1 Who is Alex to you?" in reply and "they're my partner" in reply
        argv = json.loads(rec.read_text())["argv"]
        assert "haiku" in argv                                                         # learn_call: Haiku by default
        owner = [m["text"] for m in client.get("/api/chat/messages").json()["messages"] if m["role"] == "owner"]
        assert owner[-1] == "(spoken, on a call) they're my partner"                  # stored as said, no notes
