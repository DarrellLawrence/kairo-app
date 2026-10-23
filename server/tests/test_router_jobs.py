"""G11: the router in front of the model calls. Jobs (Jobs.ask_brain) may go local once the lab has passed them; owner turns
(BrainRunner.stream) are logged and always the subscription. With g11.router off nothing changes. Made-up data only."""

from __future__ import annotations

import asyncio

import pytest
import yaml
from sqlmodel import Session, select

from app import flags, router
from app.brain import BrainEvent, EventKind
from app.brain.local_llm import LocalAnswer
from app.chat import AskResult, BrainRunner
from app.db import RouteLog, make_engine
from app.scheduler import Jobs, Skipped
from tests.test_jobs import PROFILE, Pushes

LAB = {"providers": {"claude": {"class": "R1", "enabled": True}, "local": {"class": "R0", "enabled": True}},
       "local": {"purposes": ["reply_check"], "lab_model_hash": "abc"}}


class FakeLocal:
    def __init__(self, answer=None, ready=True, model="abc"):
        self.answer = answer or LocalAnswer(True, '{"ok": true}', "ok", 3)
        self._ready, self._model, self.calls = ready, model, []

    def ready(self):
        return self._ready

    def fingerprint(self):
        return self._model

    def ask(self, prompt, system, *, timeout_s, **kw):
        self.calls.append((prompt, system, timeout_s))
        return self.answer


class Claude:
    def __init__(self, text="from claude"):
        self.text, self.calls = text, []

    def __call__(self, prompt, system, purpose="summary", model=None, images=None):
        self.calls.append((purpose, model))
        return AskResult(self.text)


@pytest.fixture
def engine(migrated):
    return make_engine(migrated.db_url)


def setup(engine, tmp_path, *, doc=LAB, flags_on=("router", "local_route"), local=None, claude=None, schedule=None, call_open=False):
    (tmp_path / "routing.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    with Session(engine) as s:
        for f in flags_on:
            flags.set_flag(s, "g11." + f, True)
    rt = router.Router(engine, tmp_path)
    claude = claude or Claude()
    jobs = Jobs(engine=engine, pusher=Pushes(), profile=PROFILE, schedule=schedule or {}, ask=claude, router=rt,
                local=local, call_open=lambda: call_open)
    return jobs, rt, claude


def log(engine):
    with Session(engine) as s:
        return s.exec(select(RouteLog).order_by(RouteLog.id)).all()


def runs_today(engine, jobs):
    with Session(engine) as s:
        return jobs.brain_state(s)["runs_today"]


# ---------------------------------------------------------------- jobs

def test_with_the_router_flag_off_a_job_goes_to_claude_exactly_as_before(engine, tmp_path):
    local = FakeLocal()
    jobs, _, claude = setup(engine, tmp_path, flags_on=())
    assert jobs.ask_brain("a mail", "rules", purpose="reply_check").text == "from claude"
    assert claude.calls == [("reply_check", "haiku")] and local.calls == [] and log(engine) == []
    assert runs_today(engine, jobs) == 1


def test_with_the_router_on_a_job_is_logged_and_still_goes_to_claude_by_default(engine, tmp_path):
    jobs, _, claude = setup(engine, tmp_path, doc={"providers": {"claude": {"class": "R1", "enabled": True}}}, local=FakeLocal())
    assert jobs.ask_brain("a mail from anna.example@mail.example", "rules", purpose="reply_check").text == "from claude"
    (row,) = log(engine)
    assert (row.purpose, row.mode, row.route, row.tier, row.model) == ("reply_check", "job", "claude", "R1", "haiku")
    assert "anna" not in " ".join(str(v) for v in row.model_dump().values()) and row.hits == "email"
    assert runs_today(engine, jobs) == 1


def test_a_lab_passed_job_runs_on_the_local_model_and_costs_no_claude_run(engine, tmp_path):
    local = FakeLocal()
    jobs, _, claude = setup(engine, tmp_path, local=local)
    res = jobs.ask_brain("a mail", "rules", purpose="reply_check")
    assert res.ok and res.text == '{"ok": true}' and claude.calls == []
    assert local.calls == [("a mail", "rules", 4.0)]                               # the small-ask timeout
    assert runs_today(engine, jobs) == 0
    (row,) = log(engine)
    assert (row.route, row.tier, row.model) == ("local", "R0", "local")
    assert jobs.ask_brain("a mail", "rules", purpose="triage").text == "from claude"   # not listed: Claude
    assert local.calls[-1][0] == "a mail" and len(local.calls) == 1


def test_a_refused_local_answer_goes_to_claude_and_is_logged_with_why(engine, tmp_path):
    local = FakeLocal(LocalAnswer(False, "", "not valid JSON", 20))
    jobs, rt, claude = setup(engine, tmp_path, local=local)
    assert jobs.ask_brain("a mail", "rules", purpose="reply_check").text == "from claude"
    assert claude.calls == [("reply_check", "haiku")] and runs_today(engine, jobs) == 1
    got = log(engine)
    assert [(r.route, r.fallback_from) for r in got] == [("local", None), ("claude", "local")]
    assert "not valid JSON" in got[1].reason and rt.fails() == 1


def test_a_local_model_that_raises_is_a_refused_answer_not_a_failed_job(engine, tmp_path):
    class Boom(FakeLocal):
        def ask(self, *a, **k):
            raise RuntimeError("segfault")
    jobs, _, claude = setup(engine, tmp_path, local=Boom())
    assert jobs.ask_brain("a mail", "rules", purpose="reply_check").text == "from claude"
    assert [r.route for r in log(engine)] == ["local", "claude"] and "RuntimeError" in log(engine)[1].reason


@pytest.mark.parametrize("what", ["flag_off", "not_ready", "call_open", "no_local", "router_off", "other_model", "unknown_model"])
def test_the_local_model_is_not_used_unless_everything_holds(engine, tmp_path, what):
    model = {"other_model": "zzz", "unknown_model": ""}.get(what, "abc")
    local = None if what == "no_local" else FakeLocal(ready=what != "not_ready", model=model)
    on = ("router",) if what == "flag_off" else () if what == "router_off" else ("router", "local_route")
    jobs, _, claude = setup(engine, tmp_path, flags_on=on, local=local, call_open=what == "call_open")
    assert jobs.ask_brain("a mail", "rules", purpose="reply_check").text == "from claude"
    assert claude.calls == [("reply_check", "haiku")]
    if local is not None:
        assert local.calls == []


def test_a_job_with_images_never_goes_local(engine, tmp_path):
    local = FakeLocal()
    jobs, _, claude = setup(engine, tmp_path, local=local)
    jobs.ask_brain("a mail", "rules", purpose="reply_check", images=[(b"png", "image/png")])
    assert local.calls == [] and claude.calls == [("reply_check", "haiku")]


def test_when_claude_is_paused_a_lab_passed_job_still_runs_locally_and_the_rest_wait(engine, tmp_path):
    local = FakeLocal()
    jobs, _, claude = setup(engine, tmp_path, local=local)
    with Session(engine) as s:
        jobs._set_kv(s, "brain_paused_until", "2999-01-01T00:00:00+00:00")
    assert jobs.ask_brain("a mail", "rules", purpose="reply_check").ok and claude.calls == []
    with pytest.raises(Skipped, match="model paused .usage limit. until"):               # the same words as before
        jobs.ask_brain("a mail", "rules", purpose="triage")
    assert [(r.route, r.purpose) for r in log(engine)] == [("local", "reply_check"), ("none", "triage")]
    assert "paused" in log(engine)[1].reason


def test_the_daily_budget_still_stops_claude_jobs_with_the_old_words(engine, tmp_path):
    jobs, _, claude = setup(engine, tmp_path, local=FakeLocal(), schedule={"brain": {"max_runs_per_day": 1}})
    assert jobs.ask_brain("m", "r", purpose="triage").ok
    with pytest.raises(Skipped, match="daily model budget of 1 runs reached"):
        jobs.ask_brain("m", "r", purpose="triage")
    assert jobs.ask_brain("m", "r", purpose="reply_check").ok                              # local: free, no budget
    assert len(claude.calls) == 1


def test_a_file_that_switches_the_subscription_off_stops_the_job_and_says_so(engine, tmp_path):
    doc = {"providers": {"claude": {"class": "R1", "enabled": False}}}
    jobs, _, claude = setup(engine, tmp_path, doc=doc)
    with pytest.raises(Skipped, match="no model route is allowed.*switched off in routing.yaml"):
        jobs.ask_brain("m", "r", purpose="reply_check")
    assert claude.calls == [] and log(engine)[0].route == "none"


def test_a_job_double_without_a_model_argument_still_works_with_the_router_on(engine, tmp_path):
    (tmp_path / "routing.yaml").write_text(yaml.safe_dump(LAB), encoding="utf-8")
    with Session(engine) as s:
        flags.set_flag(s, "g11.router", True)
    jobs = Jobs(engine=engine, pusher=Pushes(), profile=PROFILE, ask=lambda p, s_: AskResult("plain"),
                router=router.Router(engine, tmp_path))
    assert jobs.ask_brain("p", "s").text == "plain" and log(engine)[0].purpose == "summary"


# ---------------------------------------------------------------- owner turns

def owner(runner, text="hello", **kw):
    async def go():
        return [e async for e in runner.stream(text, session_id=None, system="s", tools=kw.pop("tools", ["t"]), **kw)]
    return asyncio.run(go())


class OneBrain:
    name = "fake"

    def __init__(self):
        self.runs = 0

    def run(self, prompt, **kw):
        self.runs += 1
        yield BrainEvent(EventKind.result, text="ok")


def runner_with(engine, tmp_path, doc=LAB, on=True):
    (tmp_path / "routing.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    with Session(engine) as s:
        flags.set_flag(s, "g11.router", on)
    brain = OneBrain()
    r = BrainRunner(brain)
    r.router = router.Router(engine, tmp_path)
    return r, brain


def test_an_owner_turn_is_logged_as_the_subscription_with_its_mode_and_never_stores_the_text(engine, tmp_path):
    r, brain = runner_with(engine, tmp_path)
    assert owner(r, "my IBAN is CH93 0076 2011 6238 5295 7", purpose="chat", model="haiku")[-1].text == "ok"
    owner(r, "hi", purpose="call", model="haiku")
    owner(r, "hi", purpose="money", model="opus")
    got = log(engine)
    assert [(x.purpose, x.mode, x.route, x.model, x.content) for x in got] == [
        ("chat", "owner", "claude", "haiku", "secret"), ("call", "call", "claude", "haiku", "secret"),
        ("money", "owner", "claude", "opus", "secret")]
    assert "iban" in got[0].hits and "CH93" not in " ".join(str(v) for v in got[0].model_dump().values())
    assert brain.runs == 3


def test_with_the_router_flag_off_an_owner_turn_writes_nothing(engine, tmp_path):
    r, brain = runner_with(engine, tmp_path, on=False)
    assert owner(r)[-1].text == "ok" and log(engine) == [] and brain.runs == 1


def test_a_runner_without_a_router_is_what_it_was(engine):
    brain = OneBrain()
    assert owner(BrainRunner(brain))[-1].text == "ok" and brain.runs == 1


def test_a_file_that_switches_the_subscription_off_answers_the_owner_in_words_and_runs_nothing(engine, tmp_path):
    r, brain = runner_with(engine, tmp_path, doc={"providers": {"claude": {"class": "R1", "enabled": False}}})
    events = owner(r)
    assert events[-1].kind is EventKind.error and "routing.yaml" in events[-1].text and brain.runs == 0
    assert log(engine)[0].route == "none"


def test_an_owner_turn_that_is_a_job_by_origin_still_never_goes_local(engine, tmp_path):
    local_doc = {**LAB, "local": {"purposes": ["reply_check"], "lab_model_hash": "abc"}}
    r, brain = runner_with(engine, tmp_path, doc=local_doc)
    with Session(engine) as s:
        flags.set_flag(s, "g11.local_route", True)
    owner(r, "x", purpose="reply_check", tools=[], origin="job", model="haiku")
    assert brain.runs == 1 and log(engine)[0].route == "claude"


def test_the_runner_says_whether_a_voice_line_is_open_so_the_local_model_stays_with_the_voice():
    r = BrainRunner(OneBrain())
    assert r.lines_open() == 0
    r.line_open()
    r.line_open()
    assert r.lines_open() == 2
    r.line_closed()
    r.line_closed()
    r.line_closed()                                             # one too many never goes below zero
    assert r.lines_open() == 0
