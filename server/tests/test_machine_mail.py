"""Owner's report 2026-10-20: mail a machine sent (no-reply, notifications, newsletters, mass mail, auto-submitted,
what the triage sorted as automatic mail) never causes a push, on any path: the suspicious-mail flag, a reply loop,
the morning brief. It is still triaged, labelled and listed in the app. A real person's mail notifies as before.
One decision (machine_mail.sent_by_machine), used by triage and loops."""

from __future__ import annotations

import datetime as dt
import json
import re

import pytest
from sqlmodel import Session, select

from app import brief, loop_check, loops, machine_mail, triage
from app.calendar_read import CalendarRead
from app.chat import AskResult
from app.db import OpenLoop, ReplySender, Task, TriagedEmail, make_engine
from app.executor import Executor
from app.gate import load_gate
from app.google.client import GoogleAPI
from app.profile import load_profile
from app.scheduler import Jobs
from app.settings import REPO_ROOT
from tests.fakes.fake_google import FakeGoogle, connect

PROFILE = load_profile(REPO_ROOT / "config")
PERSON = "Sam Example <sam@example.com>"
MARK = "Quarterly figures XYZ"          # the mail's subject: must never reach a push
SIGNALS = {   # each machine signal on its own; the rest of the mail looks like a person's
    "no-reply sender": {"sender": "Example Shop <no-reply@shop.example>"},
    "no-reply inside the address": {"sender": "Example Shop <orders-noreply@shop.example>"},
    "notification sender": {"sender": "Example App <notifications@app.example>"},
    "newsletter sender": {"sender": "Example News <newsletter@news.example>"},
    "List-Unsubscribe": {"headers": {"List-Unsubscribe": "<mailto:leave@lists.example>"}},
    "List-Id": {"headers": {"List-Id": "Club news <club.lists.example>"}},
    "Precedence: bulk": {"headers": {"Precedence": "bulk"}},
    "Precedence: list": {"headers": {"Precedence": "list"}},
    "Auto-Submitted": {"headers": {"Auto-Submitted": "auto-generated"}},
    "the triage sorted it as automatic mail": {"category": "ignore"},
}
HEADER_SIGNALS = [k for k, v in SIGNALS.items() if "headers" in v]
SENDER_SIGNALS = [k for k, v in SIGNALS.items() if "sender" in v]


def quiet_never():
    p = PROFILE.model_copy(deep=True)
    p.quiet_hours.start, p.quiet_hours.end = "00:00", "00:00"
    return p


class Pushes:
    def __init__(self):
        self.sent = []

    def notify(self, title, body, url="/", **kw):
        self.sent.append((title, body, url))
        return "sent"


class Brain:
    """Every job's model: the triage answers `rows`, the reply check says a person waits, the loop check passes
    everything, drafts and the brief get nothing usable (the plain brief goes out)."""

    def __init__(self, rows):
        self.rows, self.prompts = rows, []

    def __call__(self, prompt, system, purpose=None, model=None, images=None):
        self.prompts.append((purpose, prompt))
        if purpose == "triage":
            return AskResult(json.dumps(self.rows))
        if purpose == "reply_check":
            return AskResult(json.dumps({i: "yes" for i in re.findall(r'"id": "([^"]+)"', prompt)}))
        if purpose == "loop_check":
            return AskResult(json.dumps({i: {"verdict": "ok", "reason": "real"} for i in re.findall(r"open loop (\d+)", prompt)}))
        return AskResult("no")


@pytest.fixture
def env(migrated):
    fake = FakeGoogle(owner=PROFILE.gmail or "owner@example.com")
    secrets = migrated.data_dir / "secrets"
    connect(secrets, fake)
    engine = make_engine(migrated.db_url)
    read = lambda: GoogleAPI(secrets, "read", transport=fake.transport())  # noqa: E731
    from app import vault
    vault.ensure_structure(migrated.data_dir / "vault")
    return dict(fake=fake, engine=engine, read=read, secrets=secrets, vault=migrated.data_dir / "vault",
                executor=Executor(secrets_dir=secrets, transport=fake.transport()), gate=load_gate(REPO_ROOT / "config"))


def add_mail(env, mid, *, sender=PERSON, headers=None):
    env["fake"].add_message(mid, sender=sender, subject=MARK, body="Could you send me your answer by Friday?")
    for name, value in (headers or {}).items():
        env["fake"].messages[mid]["payload"]["headers"].append({"name": name, "value": value})


def learned_person(env, sender):
    """The owner once tapped Done on a loop from this address: 'you answer them'."""
    with Session(env["engine"]) as s:
        s.add(ReplySender(address=machine_mail.address(sender), verdict="person"))
        s.commit()


def run_jobs(env, rows, *, flags, pushes, jobs=("email_triage", "morning_brief", "open_loops")):
    """Triage, then the morning brief, then the loop engine, as on the PC (with the real gate and executor)."""
    brain = Brain(rows)
    t = triage.Triage(engine=env["engine"], gate=env["gate"], executor=env["executor"], google_read=env["read"],
                      on_flag=lambda m: flags.append(m.message_id))   # what main.py pushes
    maker = brief.BriefMaker(engine=env["engine"], google_read=env["read"], vault_dir=env["vault"], profile=PROFILE)
    cal = lambda day: CalendarRead(ok=True, events=[], days=7)  # noqa: E731
    engine = loops.LoopEngine(engine=env["engine"], profile=quiet_never(), schedule={}, google_read=env["read"],
                              calendar=cal, pusher=pushes, vault_dir=env["vault"], config_dir=REPO_ROOT / "config",
                              checker=loop_check.LoopChecker(profile=PROFILE, google_read=env["read"], calendar=cal))
    j = Jobs(engine=env["engine"], pusher=pushes, profile=quiet_never(), schedule={}, ask=brain,
             bodies={"email_triage": t.run, "morning_brief": lambda ctx: maker.make(ctx), "open_loops": engine.run})
    for name in jobs:
        run = j.run_job(name, forced=True)
        assert run.status in ("ok", "skipped"), (name, run.error)
    return brain


def row(mid, *, category="reply", suspicious=False):
    return {"id": mid, "category": category, "summary": f"Sam wants the {MARK} by Friday", "deadline": "2026-10-23",
            "suspicious": suspicious, "reason": "asks to log in somewhere" if suspicious else ""}


# ---------------------------------------------------------------- the one decision

@pytest.mark.parametrize("signal", SIGNALS)
def test_each_machine_signal_on_its_own(signal):
    sig = SIGNALS[signal]
    assert machine_mail.sent_by_machine(sig.get("sender", PERSON), sig.get("headers", {}), sig.get("category", "reply"))


def test_a_person_is_not_a_machine():
    assert machine_mail.sent_by_machine(PERSON, {}, "reply") is None
    assert machine_mail.sent_by_machine(PERSON, {"Auto-Submitted": "no", "Precedence": "first-class"}, "fyi") is None
    assert machine_mail.sent_by_machine("Mr. Keller <keller@school.example>", None, None) is None
    # headers read lazily: only when the sender and the triage's sort didn't decide
    asked = []
    assert machine_mail.sent_by_machine("Example Shop <no-reply@shop.example>", lambda: asked.append(1) or {}) and not asked
    assert machine_mail.sent_by_machine(PERSON, lambda: {"list-id": "x"}) == "bulk mail headers"


# ---------------------------------------------------------------- the suspicious-mail flag

@pytest.mark.parametrize("signal", SIGNALS)
def test_a_machine_mail_flagged_suspicious_is_listed_but_never_pushed(env, signal):
    sig = SIGNALS[signal]
    add_mail(env, "m", sender=sig.get("sender", PERSON), headers=sig.get("headers"))
    flags, pushes = [], Pushes()
    run_jobs(env, [row("m", category=sig.get("category", "reply"), suspicious=True)], flags=flags, pushes=pushes)
    assert flags == []                                                          # no "Suspicious mail flagged" push
    assert all(MARK not in body for _, body, _ in pushes.sent)
    names = {v: k for k, v in env["fake"].labels.items()}
    assert "Kairo/Suspicious" in {names.get(lab, lab) for lab in env["fake"].messages["m"]["labelIds"]}   # still labelled
    with Session(env["engine"]) as s:
        assert [m.message_id for m in triage.flagged(s)] == ["m"]                # and still listed in the app


def test_a_real_persons_suspicious_mail_still_pushes(env):
    add_mail(env, "m")
    flags = []
    run_jobs(env, [row("m", suspicious=True)], flags=flags, pushes=Pushes(), jobs=("email_triage",))
    assert flags == ["m"]


# ---------------------------------------------------------------- reply loops and the brief

@pytest.mark.parametrize("signal", SIGNALS)
def test_a_machine_mail_never_pushes_through_a_loop_a_task_or_the_brief(env, signal):
    """Even from an address the owner once answered: the machine signal decides."""
    sig = SIGNALS[signal]
    sender = sig.get("sender", PERSON)
    add_mail(env, "m", sender=sender, headers=sig.get("headers"))
    learned_person(env, sender)
    pushes = Pushes()
    brain = run_jobs(env, [row("m", category=sig.get("category", "reply"))], flags=[], pushes=pushes)
    assert [t for t, _, _ in pushes.sent] == ["Morning brief"]                  # only the brief's own push
    assert all(MARK not in body and "mail to answer" not in body for _, body, _ in pushes.sent)
    brief_prompt = "".join(p for purpose, p in brain.prompts if purpose == "brief")
    assert MARK not in brief_prompt                                             # not a brief item either
    with Session(env["engine"]) as s:
        m = s.get(TriagedEmail, "m")
        assert m is not None and m.personal_reply is not True                  # still triaged and listed
        assert s.exec(select(Task)).all() == []                                 # no task from it
        assert [lp for lp in s.exec(select(OpenLoop)).all() if lp.status == "open"] == []
        assert triage.needs_reply(s) == []


def test_a_real_persons_mail_notifies_as_before(env):
    add_mail(env, "m")
    learned_person(env, PERSON)
    pushes = Pushes()
    brain = run_jobs(env, [row("m")], flags=[], pushes=pushes)
    titles = [t for t, _, _ in pushes.sent]
    assert titles == ["Morning brief", "Kairo"]
    assert "1 mail to answer" in pushes.sent[0][1]                              # the brief counts it
    assert "Sam Example" in pushes.sent[1][1] and pushes.sent[1][2].startswith("/#loops/")   # and the loop pings
    assert MARK in "".join(p for purpose, p in brain.prompts if purpose == "brief")
    with Session(env["engine"]) as s:
        assert [t.title for t in s.exec(select(Task)).all()] == [f"{MARK} (from Sam Example)"]
        assert [m.message_id for m in triage.needs_reply(s)] == ["m"]


def test_a_real_persons_mail_the_reply_check_passes_pings_too(env):
    """No learned sender: the reply check (model) decides, as before."""
    add_mail(env, "m")
    pushes = Pushes()
    run_jobs(env, [row("m")], flags=[], pushes=pushes, jobs=("email_triage", "open_loops"))
    assert [t for t, _, _ in pushes.sent] == ["Kairo"]


@pytest.mark.parametrize("signal", SENDER_SIGNALS + HEADER_SIGNALS)
def test_old_machine_loops_are_closed_never_pinged(env, signal):
    """A loop from before (the owner had once answered that address): closed as automated, no push, not in
    'needs a reply'."""
    sig = SIGNALS[signal]
    sender = sig.get("sender", PERSON)
    add_mail(env, "m", sender=sender, headers=sig.get("headers"))
    learned_person(env, sender)
    now = dt.datetime.now(dt.timezone.utc)
    with Session(env["engine"]) as s:
        s.add(TriagedEmail(message_id="m", thread_id="t-m", sender=sender, subject=MARK, summary=f"wants the {MARK}",
                           category="reply", received_at=now - dt.timedelta(hours=3), triaged_at=now,
                           personal_reply=True, reply_why="you answer them"))
        s.add(OpenLoop(kind="reply", source="email", source_ref="m", who="Sam", who_email=machine_mail.address(sender),
                       what=f"wants the {MARK}", since=now - dt.timedelta(hours=3), next_at=now - dt.timedelta(minutes=1)))
        s.commit()
    pushes = Pushes()
    run_jobs(env, [], flags=[], pushes=pushes, jobs=("open_loops", "morning_brief"))
    assert [t for t, _, _ in pushes.sent] == ["Morning brief"]
    assert all(MARK not in body and "mail to answer" not in body for _, body, _ in pushes.sent)
    with Session(env["engine"]) as s:
        lp = s.exec(select(OpenLoop)).one()
        assert (lp.status, lp.close_reason) == ("stale", "automated")
        assert s.get(TriagedEmail, "m").personal_reply is False and triage.needs_reply(s) == []


# ---------------------------------------------------------------- one shared decision

def test_triage_and_loops_ask_the_one_shared_decision(env, monkeypatch):
    """Both call machine_mail.sent_by_machine: told a real person's mail is a machine's, neither pushes."""
    monkeypatch.setattr(machine_mail, "sent_by_machine", lambda *a, **k: "test")
    add_mail(env, "sus")
    add_mail(env, "m")
    learned_person(env, PERSON)
    flags, pushes = [], Pushes()
    run_jobs(env, [row("sus", suspicious=True), row("m")], flags=flags, pushes=pushes)
    assert flags == [] and [t for t, _, _ in pushes.sent] == ["Morning brief"]
    with Session(env["engine"]) as s:
        assert s.get(TriagedEmail, "m").reply_why == "automated (test)"        # loops.rules_verdict, from triage
        s.add(TriagedEmail(message_id="old", sender=PERSON, subject="x", category="reply", personal_reply=None))
        s.commit()
        assert loops.rules_verdict(s, s.get(TriagedEmail, "old")) == (False, "automated (test)")
    assert loops.automated_sender is machine_mail.automated_sender                # one copy of the rules
