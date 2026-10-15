"""Fix plan 28: the owner goes first. Chat, calls and voice notes get the brain before waiting
jobs; a job call that is running is stopped and redone after the owner's turn."""

import asyncio
import threading
import time

from app.brain import BrainEvent, EventKind
from app.chat import BrainRunner, ask_sync


class FakeBrain:
    """A prompt starting with "slow" takes 20 s the first time (unless cancelled), then is quick."""

    name = "fake"

    def __init__(self) -> None:
        self.log: list[tuple[str, str]] = []
        self.cancels = 0
        self._cancel = threading.Event()
        self.started = threading.Event()
        self._slow_seen: set[str] = set()

    def run(self, prompt, *, session_id, system, tools, origin="owner_chat", **kw):
        self._cancel.clear()          # like the real adapter: a cancel before the run starts is lost
        self.log.append(("start", prompt))
        self.started.set()
        if prompt.startswith("slow") and prompt not in self._slow_seen:
            self._slow_seen.add(prompt)
            if self._cancel.wait(20):
                self.log.append(("stopped", prompt))
                yield BrainEvent(EventKind.error, text="Stopped by the kill switch.", data={"cancelled": True})
                return
        self.log.append(("done", prompt))
        yield BrainEvent(EventKind.result, text="ok " + prompt)

    def cancel(self) -> None:
        self.cancels += 1
        self._cancel.set()


def owner_turn(runner: BrainRunner, text: str) -> list[BrainEvent]:
    async def go():
        return [e async for e in runner.stream(text, session_id=None, system="s", tools=[])]
    return asyncio.run(go())


def test_the_owner_stops_a_running_job_call_and_the_job_runs_again_after():
    brain = FakeBrain()
    runner = BrainRunner(brain)
    result = {}
    job = threading.Thread(target=lambda: result.setdefault("r", ask_sync(runner, "slow triage", system="s")))
    job.start()
    assert brain.started.wait(5)
    t0 = time.monotonic()
    events = owner_turn(runner, "hi")
    assert events[-1].text == "ok hi"
    assert time.monotonic() - t0 < 10            # not the job's 20 s
    job.join(10)
    assert result["r"].ok and result["r"].text == "ok slow triage"
    assert brain.log == [("start", "slow triage"), ("stopped", "slow triage"), ("start", "hi"), ("done", "hi"),
                         ("start", "slow triage"), ("done", "slow triage")]


def test_the_owner_goes_before_jobs_that_are_waiting():
    brain = FakeBrain()
    runner = BrainRunner(brain)
    release = threading.Event()

    def busy_job():               # holds the brain and can't be stopped (as after two stops)
        with runner.hold(owner=False):
            release.wait(10)

    holder = threading.Thread(target=busy_job)
    holder.start()
    time.sleep(0.2)
    waiting_job = threading.Thread(target=lambda: ask_sync(runner, "next job", system="s"))
    waiting_job.start()
    time.sleep(0.3)               # the job is waiting first ...
    owner = threading.Thread(target=lambda: owner_turn(runner, "owner"))
    owner.start()
    time.sleep(0.3)               # ... then the owner
    assert brain.cancels == 0     # a job that can't be stopped isn't
    release.set()
    for t in (holder, waiting_job, owner):
        t.join(10)
    assert [p for kind, p in brain.log if kind == "start"] == ["owner", "next job"]


def test_the_kill_switch_stops_a_job_without_running_it_again():
    brain = FakeBrain()
    runner = BrainRunner(brain)
    result = {}
    job = threading.Thread(target=lambda: result.setdefault("r", ask_sync(runner, "slow digest", system="s")))
    job.start()
    assert brain.started.wait(5)
    runner.cancel()
    job.join(10)
    assert not result["r"].ok
    assert [p for kind, p in brain.log if kind == "start"] == ["slow digest"]


def test_a_job_holds_the_brain_for_one_call_at_a_time():
    """Between two calls of the same job, a waiting owner gets in."""
    brain = FakeBrain()
    runner = BrainRunner(brain)
    gate = threading.Event()
    order = []

    def job():
        ask_sync(runner, "first", system="s")
        gate.wait(5)
        ask_sync(runner, "second", system="s")
        order.append("job done")

    t = threading.Thread(target=job)
    t.start()
    time.sleep(0.2)
    owner = threading.Thread(target=lambda: (owner_turn(runner, "owner"), order.append("owner done")))
    owner.start()
    time.sleep(0.2)
    gate.set()
    t.join(10)
    owner.join(10)
    starts = [p for kind, p in brain.log if kind == "start"]
    assert starts.index("owner") < starts.index("second")


def test_a_job_call_never_runs_unstoppable_while_the_owner_talks():
    """Voice V1: the owner never waits for a job. A job stopped too often gives up for now (it runs
    again on its next schedule) instead of finishing first."""
    brain = FakeBrain()
    runner = BrainRunner(brain)
    result = {}
    job = threading.Thread(target=lambda: result.setdefault("r", ask_sync(runner, "slow 1", system="s")))
    brain._slow_seen.clear()
    job.start()
    for i in range(3):                               # the owner talks over every try of the job
        assert brain.started.wait(5)
        brain.started.clear()
        while ("start", "slow 1") not in brain.log[-1:]:
            time.sleep(0.01)
        brain._slow_seen.discard("slow 1")           # it would be slow again
        t0 = time.monotonic()
        owner_turn(runner, f"owner {i}")
        assert time.monotonic() - t0 < 5
    job.join(10)
    assert not result["r"].ok and "too often" in result["r"].error


def test_jobs_wait_while_the_owner_is_in_a_conversation():
    """After an owner turn, jobs keep off the brain for a quiet moment (the owner may go on talking)."""
    brain = FakeBrain()
    runner = BrainRunner(brain, quiet_s=0.6)
    owner_turn(runner, "owner")
    t0 = time.monotonic()
    ask_sync(runner, "job", system="s")
    assert time.monotonic() - t0 >= 0.5
    assert [p for kind, p in brain.log if kind == "start"] == ["owner", "job"]


def test_jobs_wait_while_a_voice_line_is_open():
    brain = FakeBrain()
    runner = BrainRunner(brain, quiet_s=0.0)
    runner.line_open()
    done = threading.Event()
    job = threading.Thread(target=lambda: (ask_sync(runner, "job", system="s"), done.set()))
    job.start()
    assert not done.wait(0.8)                        # a call is on: the job waits
    runner.line_closed()
    assert done.wait(5)
