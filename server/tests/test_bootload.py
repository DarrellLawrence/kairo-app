"""G8: the loads that need the graphics card start one after the other (voice recipe 9): the voice model (llama.cpp), then Whisper and
Kokoro, then the memory model, each after the previous one has settled and the free graphics memory has been read. Before, Kairo started
all of them at once, so the memory model's "is there 3000 MB free?" check looked at the card before llama.cpp had taken its share."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from sqlmodel import Session

from app import bootload, flags, semantic
from app.db import make_engine
from app.main import create_app


def run(coro, timeout=10):
    return asyncio.run(asyncio.wait_for(coro, timeout))


class Card:
    """A graphics card whose free memory drops when a stage 'loads'."""

    def __init__(self, free, events):
        self.free, self.events = free, events

    def read(self):
        self.events.append(f"free {self.free}")
        return self.free


def test_each_stage_waits_until_the_one_before_has_settled():
    events, polls = [], {"a": 0}

    def a_settled():
        polls["a"] += 1
        events.append(f"a settled? {polls['a']}")
        return polls["a"] >= 3

    stages = [bootload.Stage("a", start=lambda: events.append("a start"), settled=a_settled, timeout_s=5),
              bootload.Stage("b", start=lambda: events.append("b start"))]
    run(bootload.run(stages, free_mb=lambda: None, poll_s=0.001))
    assert events == ["a start", "a settled? 1", "a settled? 2", "a settled? 3", "b start"]


def test_free_graphics_memory_is_read_before_and_after_each_stage_and_the_trail_says_what_each_took():
    events = []
    card = Card(11000, events)
    stages = [bootload.Stage("llama", start=lambda: (events.append("llama"), setattr(card, "free", 7500))),
              bootload.Stage("voice", start=lambda: (events.append("voice"), setattr(card, "free", 5200)))]
    trail = []
    got = run(bootload.run(stages, free_mb=card.read, trail=trail, poll_s=0.001))
    assert events == ["free 11000", "llama", "free 7500", "free 7500", "voice", "free 5200"]
    assert got is trail
    assert [(t["stage"], t["free_before_mb"], t["free_after_mb"], t["used_mb"]) for t in trail] == \
        [("llama", 11000, 7500, 3500), ("voice", 7500, 5200, 2300)]
    assert all(t["settled"] is True and t["error"] is None and t["ms"] >= 0 for t in trail)


def test_a_card_that_does_not_say_leaves_the_numbers_empty_and_the_order_the_same():
    trail = run(bootload.run([bootload.Stage("a", start=lambda: None)], free_mb=lambda: None, poll_s=0.001))
    assert trail[0]["free_before_mb"] is None and trail[0]["free_after_mb"] is None and trail[0]["used_mb"] is None


def test_the_trail_is_filled_as_it_goes_so_a_page_can_show_the_boot_in_progress():
    trail, seen = [], []
    stages = [bootload.Stage("a", start=lambda: None),
              bootload.Stage("b", start=lambda: seen.append([t["stage"] for t in trail]))]
    run(bootload.run(stages, free_mb=lambda: None, trail=trail, poll_s=0.001))
    assert seen == [["a"]]


def test_a_stage_that_fails_is_recorded_and_the_next_one_still_runs():
    def boom():
        raise RuntimeError("CUDA out of memory")
    events = []
    trail = run(bootload.run([bootload.Stage("a", start=boom), bootload.Stage("b", start=lambda: events.append("b"))],
                             free_mb=lambda: None, poll_s=0.001))
    assert events == ["b"]
    assert trail[0]["error"] == "RuntimeError: CUDA out of memory" and trail[0]["settled"] is False and trail[1]["error"] is None


def test_a_stage_that_never_settles_does_not_hold_the_others_up_for_ever():
    events = []
    stages = [bootload.Stage("a", start=lambda: None, settled=lambda: False, timeout_s=0.05),
              bootload.Stage("b", start=lambda: events.append("b"))]
    trail = run(bootload.run(stages, free_mb=lambda: None, poll_s=0.01))
    assert events == ["b"] and trail[0]["settled"] is False and trail[0]["timed_out"] is True and trail[1]["timed_out"] is False


def test_a_start_that_hangs_is_left_running_and_the_sequence_goes_on():
    hold, events = threading.Event(), []
    stages = [bootload.Stage("a", start=lambda: hold.wait(0.3), timeout_s=0.05),
              bootload.Stage("b", start=lambda: events.append("b"))]
    try:
        trail = run(bootload.run(stages, free_mb=lambda: None, poll_s=0.01))
    finally:
        hold.set()
    assert events == ["b"] and trail[0]["timed_out"] is True


def test_shutting_down_during_a_stage_cancels_the_sequence():
    async def go():
        hold = threading.Event()
        task = asyncio.create_task(bootload.run([bootload.Stage("a", start=lambda: None, settled=lambda: False, timeout_s=30),
                                                 bootload.Stage("b", start=hold.set)], free_mb=lambda: None, poll_s=0.01))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not hold.is_set()
    run(go())


def test_the_log_line_per_stage_names_the_stage_and_the_numbers(caplog):
    caplog.set_level("INFO", logger="kairo.boot")
    run(bootload.run([bootload.Stage("voice model", start=lambda: None)], free_mb=iter([9000, 6000]).__next__, poll_s=0.001))
    line = next(r.getMessage() for r in caplog.records if "voice model" in r.getMessage())
    assert "9000" in line and "6000" in line and "3000" in line


# ---------------------------------------------------------------- the wiring in Kairo's start

class FakeVoiceModel:
    def __init__(self, events, settle_after=3):
        self.events, self.polls, self.settle_after = events, 0, settle_after

    def ensure(self):
        self.events.append("llama ensure")
        return "starting"

    def watch(self):
        self.events.append("llama watch")

    def status(self):
        self.polls += 1
        return {"state": "ready" if self.polls >= self.settle_after else "starting", "model": "m", "error": None}


class FakeVoice:
    def __init__(self, events):
        self.events = events

    def warm(self):
        self.events.append("voice warm")

    def devices(self):
        return {}


class FakeSpeaker:
    def __init__(self, events):
        self.events = events

    def warm(self):
        self.events.append("speaker warm")


class FakeEmbedder:
    def __init__(self, events):
        self.events = events

    def gate_opened(self):
        self.events.append("embedder released")


class FakeMemory:
    def __init__(self, events):
        self.events, self.embedder = events, FakeEmbedder(events)

    def reindex(self):
        self.events.append("memory reindex")
        return {"changed": 0, "removed": 0, "embedded": 0}


@pytest.fixture
def app_with_fakes(migrated, monkeypatch):
    events = []
    app = create_app(migrated)
    app.state.voice_llm = FakeVoiceModel(events)
    app.state.voice = FakeVoice(events)
    app.state.speaker = FakeSpeaker(events)
    app.state.memory = FakeMemory(events)
    monkeypatch.setattr(bootload, "POLL_S", 0.005)
    return app, events


async def until(events, what, timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        if what in events:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{what!r} never happened: {events}")


def boot_and_stop(app, events, wait_for="embedder released"):
    async def go():
        task = asyncio.create_task(app.state.boot_loads())
        await until(events, wait_for)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    run(go())


def test_the_flag_is_on_by_default_and_says_what_it_does():
    on, what = flags.FLAGS["g8.stagger_boot"]
    assert on is True and "one after the other" in what


def test_with_the_flag_on_the_voice_model_comes_first_then_whisper_and_kokoro_then_the_memory_model_is_let_in(app_with_fakes):
    app, events = app_with_fakes
    boot_and_stop(app, events)
    order = [events.index(e) for e in ("llama ensure", "llama watch", "voice warm", "speaker warm", "embedder released")]
    assert order == sorted(order)
    trail = app.state.boot_trail
    assert [t["stage"] for t in trail] == ["voice model (llama.cpp)", "voice (Whisper, Kokoro)"]
    assert trail[0]["settled"] is True


def test_the_keyword_index_does_not_wait_for_the_card_but_the_memory_model_does(app_with_fakes):
    app, events = app_with_fakes
    app.state.voice_llm.settle_after = 10_000            # the voice model is still loading

    async def go():
        task = asyncio.create_task(app.state.boot_loads())
        await until(events, "memory reindex")
        await asyncio.sleep(0.1)
        before = (list(events), app.state.memory_gate.is_set())
        app.state.voice_llm.settle_after = 0             # now it has loaded
        await until(events, "embedder released")
        after = app.state.memory_gate.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return before, after
    (before, gate_before), gate_after = run(go())
    assert "voice warm" not in before and "embedder released" not in before and gate_before is False
    assert gate_after is True


def test_whisper_and_kokoro_load_while_the_memory_model_is_still_held_back(app_with_fakes):
    app, events = app_with_fakes
    seen = []
    original = app.state.voice.warm
    app.state.voice.warm = lambda: (seen.append(app.state.memory_gate.is_set()), original())
    boot_and_stop(app, events)
    assert seen == [False]


def test_with_the_flag_off_everything_starts_at_once_as_before(app_with_fakes):
    app, events = app_with_fakes
    app.state.voice_llm.settle_after = 10_000           # the voice model is still loading ...
    with Session(make_engine(app.state.settings.db_url)) as s:
        flags.set_flag(s, "g8.stagger_boot", False)
    boot_and_stop(app, events, wait_for="memory reindex")   # ... and the memory model does not wait for it
    assert "llama watch" in events and "llama ensure" not in events
    assert app.state.memory_gate.is_set()
    assert app.state.boot_trail == [] and "embedder released" not in events


def test_a_stopped_boot_leaves_the_memory_model_free_to_start(app_with_fakes):
    app, events = app_with_fakes
    app.state.voice_llm.settle_after = 10_000

    async def go():
        task = asyncio.create_task(app.state.boot_loads())
        await until(events, "memory reindex")
        assert app.state.memory_gate.is_set() is False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    run(go())
    assert app.state.memory_gate.is_set() is True


WORKER = "import json, sys\nsys.stdout.write(json.dumps({'ready': True, 'device': 'cpu', 'model': 'x'}) + '\\n'); sys.stdout.flush()\nsys.stdin.read()\n"


def test_the_memory_model_is_not_started_while_the_gate_is_closed_and_starts_when_it_opens_if_it_was_asked_for(tmp_path, monkeypatch):
    models = tmp_path / "models"
    models.mkdir()
    (models / "tokenizer.json").write_text("{}", encoding="utf-8")
    script = tmp_path / "w.py"
    script.write_text(WORKER, encoding="utf-8")
    monkeypatch.setattr(semantic, "WORKER", script)
    gate = threading.Event()
    e = semantic.Embedder(models, may_start=gate.is_set)
    try:
        e.gate_opened()                                   # nobody asked: nothing starts
        assert not e.running() and not e._warming
        with pytest.raises(semantic.NotReady):
            e._start()
        with pytest.raises(semantic.NotReady):            # a chat search in that time: keywords only
            e.rank(tmp_path / "index.sqlite", "x")
        assert not e.running()
        gate.set()
        e.gate_opened()                                   # it was asked for: now
        for _ in range(200):
            if e.running():
                break
            time.sleep(0.05)
        assert e.running() and e.device == "cpu"
    finally:
        e.close()


EMBED_WORKER = (
    "import base64, json, struct, sys\n"
    "sys.stdout.write(json.dumps({'ready': True, 'device': 'cpu', 'model': 'x'}) + '\\n'); sys.stdout.flush()\n"
    "for line in sys.stdin:\n"
    "    req = json.loads(line)\n"
    "    vecs = [struct.pack('<2f', 1.0, 0.0) if 'raining' in t else struct.pack('<2f', 0.0, 1.0) for t in req['texts']]\n"
    "    sys.stdout.write(json.dumps({'ok': True, 'dim': 2, 'b64': base64.b64encode(b''.join(vecs)).decode()}) + '\\n'); sys.stdout.flush()\n")


def test_an_explicit_self_test_never_waits_for_the_boot_order(tmp_path, monkeypatch):
    """The installer's self-test runs in the first minutes after Kairo starts, while the gate is still closed: it must get its
    answer (otherwise a good install would be rolled back as 'memory broken')."""
    models = tmp_path / "models"
    models.mkdir()
    (models / "tokenizer.json").write_text("{}", encoding="utf-8")
    script = tmp_path / "w.py"
    script.write_text(EMBED_WORKER, encoding="utf-8")
    monkeypatch.setattr(semantic, "WORKER", script)
    e = semantic.Embedder(models, may_start=lambda: False)
    try:
        with pytest.raises(semantic.NotReady):
            e.embed(["x"])                                 # background use: held back
        got = e.selftest()
        assert got["ok"] is True and got["device"] == "cpu"
    finally:
        e.close()
