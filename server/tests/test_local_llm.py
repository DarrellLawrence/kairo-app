"""G11: the local route's adapter. It reaches the model only through voice_local.VoiceModel.stream() (127.0.0.1) and
accepts an answer only when it is complete, valid JSON. Fake models only; the real one needs the PC (router lab, P21)."""

from __future__ import annotations

import asyncio
import threading
import time

from app import outbound
from app.brain import local_llm
from app.settings import REPO_ROOT


class FakeModel:
    """What VoiceModel offers: ready() and an async stream() of {"kind": "text"|"done", ...} events."""

    def __init__(self, pieces=('{"a": 1}',), finish="stop", delay=0.0, error=None, ready=True):
        self.pieces, self.finish, self.delay, self.error, self._ready = pieces, finish, delay, error, ready
        self.calls = []

    def ready(self):
        return self._ready

    async def stream(self, messages, *, tools=None, temperature=0.6, max_tokens=300):
        self.calls.append({"messages": messages, "tools": tools, "temperature": temperature, "max_tokens": max_tokens})
        if self.error:
            raise self.error
        for p in self.pieces:
            await asyncio.sleep(self.delay)
            yield {"kind": "text", "text": p}
        yield {"kind": "done", "finish": self.finish}


def ask(model, prompt="p", **kw):
    return local_llm.LocalLLM(model).ask(prompt, "system", timeout_s=kw.pop("timeout_s", 5), **kw)


def test_a_complete_json_answer_is_accepted_and_asked_without_tools_at_temperature_zero():
    m = FakeModel(pieces=('```json\n{"verdict"', ': "yes"}\n```'))
    a = ask(m, "mail text")
    assert a.ok and a.reason == "ok" and '"verdict"' in a.text and a.ms >= 0
    (call,) = m.calls
    assert call["tools"] is None and call["temperature"] == 0.0
    assert call["messages"] == [{"role": "system", "content": "system"}, {"role": "user", "content": "mail text"}]


def test_an_answer_that_is_not_json_or_was_cut_off_or_empty_is_refused_with_the_reason():
    assert ask(FakeModel(pieces=("I think the mail needs a reply.",))).reason == "not valid JSON"
    assert ask(FakeModel(pieces=('{"a": ',), finish="length")).reason == "cut off at the length limit"
    assert ask(FakeModel(pieces=("",))).reason == "empty answer"
    assert ask(FakeModel(pieces=("42",))).reason == "not valid JSON"          # a bare number is not an answer to a job
    for bad in ("I think the mail needs a reply.", "42"):
        assert not ask(FakeModel(pieces=(bad,))).ok


def test_a_slow_or_broken_model_is_refused_not_raised():
    slow = ask(FakeModel(pieces=('{"a":', '1}'), delay=0.5), timeout_s=0.2)
    assert not slow.ok and slow.reason == "no answer within 0.2 s"
    broken = ask(FakeModel(error=RuntimeError("voice model said 500: boom")))
    assert not broken.ok and broken.reason.startswith("RuntimeError") and "boom" not in broken.reason   # no server text in the log
    assert not local_llm.LocalLLM(FakeModel(ready=False)).ready() and local_llm.LocalLLM(FakeModel()).ready()


def test_a_prompt_too_long_for_the_voice_models_context_is_refused_before_it_is_sent():
    m = FakeModel()
    a = ask(m, "x" * (local_llm.MAX_PROMPT_CHARS + 1))
    assert not a.ok and a.reason == "prompt too long for the local model" and m.calls == []


def test_it_works_inside_a_running_event_loop_too():
    out = []

    async def inside():
        out.append(ask(FakeModel()))

    asyncio.run(inside())
    assert out[0].ok


def test_it_works_from_a_worker_thread_like_a_job_does():
    out = []
    t = threading.Thread(target=lambda: out.append(ask(FakeModel())))
    t.start()
    t.join(5)
    assert out and out[0].ok


def test_the_adapter_has_no_network_code_of_its_own_and_is_not_an_outbound_module():
    src = (REPO_ROOT / "server" / "app" / "brain" / "local_llm.py").read_text(encoding="utf-8")
    assert not any(k in src for k in ("httpx", "urllib", "requests", "socket", "http.client", "websockets"))
    assert "brain/local_llm.py" not in outbound.MODULES


def test_no_tools_and_no_images_can_be_passed_in():
    import inspect
    params = inspect.signature(local_llm.LocalLLM.ask).parameters
    assert "tools" not in params and "images" not in params


def test_a_voice_model_that_never_finishes_cannot_hold_a_job_longer_than_the_timeout():
    t0 = time.monotonic()
    a = ask(FakeModel(pieces=("x",) * 50, delay=0.2), timeout_s=0.3)
    assert not a.ok and time.monotonic() - t0 < 3
