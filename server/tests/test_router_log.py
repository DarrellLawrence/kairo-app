"""G11: the router with a database: both flags (off and on), the decision log, names from the database, the breaker for
the local model, and what a failed router does (nothing: the subscription runs as before). Made-up data only."""

from __future__ import annotations

import datetime as dt

import pytest
import yaml
from sqlmodel import Session, select

from app import flags, router
from app.db import Person, RouteLog, make_engine, utcnow
from app.settings import REPO_ROOT

LAB = {"providers": {"claude": {"class": "R1", "enabled": True}, "local": {"class": "R0", "enabled": True}},
       "local": {"purposes": ["triage", "reply_check"], "lab_model_hash": "abc"}}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def engine(migrated):
    return make_engine(migrated.db_url)


def make(engine, tmp_path, doc=None, **kw):
    if doc is not None:
        (tmp_path / "routing.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
        return router.Router(engine, tmp_path, **kw)
    return router.Router(engine, REPO_ROOT / "config", **kw)


def set_flags(engine, **values):
    with Session(engine) as s:
        for name, on in values.items():
            flags.set_flag(s, "g11." + name, on)


def rows(engine):
    with Session(engine) as s:
        return s.exec(select(RouteLog).order_by(RouteLog.id)).all()


# ---------------------------------------------------------------- the flags

def test_both_flags_are_off_by_default_and_say_in_plain_words_what_they_do():
    for name in ("g11.router", "g11.local_route"):
        default, what = flags.FLAGS[name]
        assert default is False and len(what) > 60 and "Claude" in what, name
    assert "router lab" in flags.FLAGS["g11.local_route"][1] and "PC" in flags.FLAGS["g11.local_route"][1]
    assert [n for n in flags.FLAGS if n.startswith("g11.")] == ["g11.router", "g11.local_route"]


def test_with_the_router_flag_off_nothing_is_scanned_or_written(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(router, "scan", lambda *a, **k: pytest.fail("scanned"))
    r = make(engine, tmp_path)
    d = r.route("triage", "a mail from anna.example@mail.example", mode="job", model="haiku")
    assert (d.route, d.model) == ("claude", "haiku") and "g11.router is off" in d.reason
    assert rows(engine) == []


def test_with_the_router_flag_on_every_decision_is_written_with_its_reason(engine, tmp_path):
    set_flags(engine, router=True)
    r = make(engine, tmp_path)
    d = r.route("triage", "plain words", mode="job", model="haiku")
    r.route("chat", "hello", mode="owner", model="sonnet", tools=True)
    got = rows(engine)
    assert [(x.purpose, x.mode, x.route, x.tier, x.model, x.content) for x in got] == [
        ("triage", "job", "claude", "R1", "haiku", "personal"), ("chat", "owner", "claude", "R1", "sonnet", "secret")]
    assert got[0].reason == d.reason and "default" in got[0].reason and got[0].ts is not None
    assert got[0].hits in (None, "") and got[0].fallback_from is None


def test_the_prompt_text_is_never_stored_only_what_the_scanner_found(engine, tmp_path):
    set_flags(engine, router=True)
    r = make(engine, tmp_path)
    secret_text = "Pay to CH93 0076 2011 6238 5295 7 and tell anna.example@mail.example"
    d = r.route("selftest", secret_text, mode="job", model="haiku")
    assert d.content == "secret" and {"iban", "email"} <= set(d.hits)         # a public purpose, tightened by what the text holds
    (row,) = rows(engine)
    blob = " ".join(str(v) for v in row.model_dump().values())
    assert "CH93" not in blob and "anna" not in blob and "Pay to" not in blob
    assert "iban" in row.hits and "email" in row.hits and row.content == "secret"


def test_names_from_the_database_tighten_the_class(engine, tmp_path):
    set_flags(engine, router=True)
    with Session(engine) as s:
        s.add(Person(name="Maja Beispiel"))
        s.commit()
    r = make(engine, tmp_path, names=lambda: [p for p in _people(engine)])
    assert r.route("selftest", "what time is it").content == "public"
    d = r.route("selftest", "Maja said hi")
    assert d.content == "personal" and d.hits == ("name",)


def _people(engine):
    with Session(engine) as s:
        return s.exec(select(Person.name)).all()


def test_a_router_that_breaks_never_stops_the_subscription(engine, tmp_path, monkeypatch):
    set_flags(engine, router=True)
    r = make(engine, tmp_path)
    monkeypatch.setattr(router, "scan", lambda *a, **k: 1 / 0)
    d = r.route("triage", "x", model="haiku")
    assert d.route == "claude" and d.model == "haiku" and "router error" in d.reason


# ---------------------------------------------------------------- the local route

def test_the_local_route_needs_its_own_flag_the_lab_and_a_running_model(engine, tmp_path):
    r = make(engine, tmp_path, LAB)
    set_flags(engine, router=True)
    assert r.route("triage", "mail", local_ready=True, local_model="abc").route == "claude"       # g11.local_route is off
    set_flags(engine, local_route=True)
    assert r.route("triage", "mail", local_ready=False, local_model="abc").route == "claude"
    d = r.route("triage", "mail", local_ready=True, local_model="abc")
    assert (d.route, d.tier, d.model) == ("local", "R0", "local")
    assert r.route("chat", "hello", local_ready=True, local_model="abc", mode="owner", tools=True).route == "claude"
    set_flags(engine, router=False)                                            # no router, no local
    assert r.route("triage", "mail", local_ready=True, local_model="abc").route == "claude"


def test_three_failures_in_ten_minutes_open_a_breaker_for_ten_minutes(engine, tmp_path):
    clock = Clock()
    set_flags(engine, router=True, local_route=True)
    r = make(engine, tmp_path, LAB, clock=clock)
    assert r.route("triage", "mail", local_ready=True, local_model="abc").route == "local"
    for _ in range(2):
        r.local_failed()
        clock.t += 60
    assert r.route("triage", "mail", local_ready=True, local_model="abc").route == "local"        # two are not enough
    r.local_failed()
    d = r.route("triage", "mail", local_ready=True, local_model="abc")
    assert d.route == "claude" and "not running" in d.reason
    clock.t += 9 * 60
    assert r.route("triage", "mail", local_ready=True, local_model="abc").route == "claude"
    clock.t += 2 * 60
    assert r.route("triage", "mail", local_ready=True, local_model="abc").route == "local"
    r.local_failed()
    clock.t += 11 * 60                                                          # old failures age out
    r.local_failed()
    r.local_failed()
    assert r.route("triage", "mail", local_ready=True, local_model="abc").route == "local"


def test_a_rejected_local_answer_is_logged_and_goes_to_the_subscription(engine, tmp_path):
    set_flags(engine, router=True, local_route=True)
    r = make(engine, tmp_path, LAB)
    first = r.route("triage", "mail", local_ready=True, local_model="abc", model="haiku")
    second = r.escalate(first, "not valid JSON", model="haiku")
    assert (first.route, second.route, second.fallback_from) == ("local", "claude", "local")
    got = rows(engine)
    assert [x.route for x in got] == ["local", "claude"] and got[1].fallback_from == "local"
    assert "not valid JSON" in got[1].reason and got[1].model == "haiku"
    assert r.fails() == 1                                                       # and it counts toward the breaker


def test_escalating_with_the_router_off_is_the_subscription_without_a_log(engine, tmp_path):
    r = make(engine, tmp_path, LAB)
    d = r.escalate(router.Decision("local", "R0", "triage", "job", "personal", "x"), "bad", model="haiku")
    assert d.route == "claude" and rows(engine) == []


# ---------------------------------------------------------------- the log itself

def test_old_rows_are_pruned_once_a_day(engine, tmp_path):
    set_flags(engine, router=True)
    with Session(engine) as s:
        s.add(RouteLog(ts=utcnow() - dt.timedelta(days=router.KEEP_DAYS + 1), purpose="old", mode="job", content="personal",
                       route="claude", tier="R1", reason="r"))
        s.add(RouteLog(ts=utcnow() - dt.timedelta(days=3), purpose="recent", mode="job", content="personal",
                       route="claude", tier="R1", reason="r"))
        s.commit()
    r = make(engine, tmp_path)
    r.route("triage", "x")
    assert [x.purpose for x in rows(engine)] == ["recent", "triage"]


def test_the_view_lists_routes_and_why_and_the_rules_in_force(engine, tmp_path):
    set_flags(engine, router=True)
    r = make(engine, tmp_path)
    for p in ("triage", "triage", "chat"):
        r.route(p, "x", model="haiku", tools=p == "chat", mode="owner" if p == "chat" else "job")
    v = r.view(days=7)
    assert v["router_on"] is True and v["local_on"] is False and v["rules_loaded"] is True and v["problems"] == []
    assert v["total"] == 3 and v["by_route"] == {"claude": 3} and v["by_tier"] == {"R1": 3}
    assert [x["purpose"] for x in v["recent"]] == ["chat", "triage", "triage"]            # newest first
    assert v["recent"][0]["reason"] and v["recent"][0]["route"] == "claude"
    names = {p["name"]: p for p in v["providers"]}
    assert names["claude"]["status"] == "on" and names["local"]["status"].startswith("off") and "g11.local_route" in names["local"]["status"]
    assert names["groq"]["status"] == "off: switched off in routing.yaml"
    assert v["local"] == {"purposes": [], "lab_model_hash_set": False, "could_run": ["reply_check", "triage"],
                          "running_model": "", "running_model_is_lab_model": False}
    assert len(v["ceilings"]) == 33 and v["ceilings"]["selftest"] == "public" and v["ceilings"]["money"] == "secret"


def test_the_view_shows_the_running_model_so_the_owner_can_copy_it_into_the_file_after_the_lab(engine, tmp_path):
    r = make(engine, tmp_path, LAB)
    v = r.view(days=1, local_model="abc")
    assert v["local"]["running_model"] == "abc" and v["local"]["running_model_is_lab_model"] is True
    v = r.view(days=1, local_model="other")
    assert v["local"]["running_model"] == "other" and v["local"]["running_model_is_lab_model"] is False
    assert r.view(days=1)["local"]["running_model"] == ""


def test_the_view_says_so_when_the_file_is_missing_or_a_row_was_refused(engine, tmp_path):
    v = router.Router(engine, tmp_path / "nope").view(days=1)
    assert v["rules_loaded"] is False and v["router_on"] is False and v["providers"][0]["name"] == "claude"
    refused = make(engine, tmp_path, {"providers": {"claude": {"class": "R1", "enabled": True}, "groq": {"class": "R0", "enabled": True}}})
    v = refused.view(days=1)
    assert len(v["problems"]) == 1 and "groq" in v["problems"][0]
