"""The router's local route (G11): one small ask on the voice model's llama-server, for background jobs only.

OFF everywhere until the owner turns g11.local_route on and the router lab has passed a purpose (app/router.py).

This file has no network code of its own. It reaches the model only through brain/voice_local.VoiceModel.stream(),
which talks to 127.0.0.1 and nothing else, so it is not in outbound.MODULES. It cannot be given tools or images
(a job with either never goes local, see router.decide), and it accepts an answer only when it is complete, is valid
JSON (nothing around it) and passes the purpose's own check (ANSWER_CHECKS: the right shape, every item answered, only
allowed values). Anything else is refused with a reason and the job goes to Claude as before (Router.escalate).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

MAX_PROMPT_CHARS = 12_000       # about 3-4k tokens: a job's slot has voice_local.JOB_MIN_CONTEXT (6144) tokens or more
MAX_TOKENS = 700
CALL_POLL_S = 0.05              # how often a running job looks whether a call has opened
CALL_OPENED = "a call opened: the model stays with the voice"
NO_JOB_SLOT = ("the voice model's server has one slot, and a job on it would wipe the mouth's cached start and make a call "
               "wait; the local route needs a second slot for jobs (-np 2) on the server")


@dataclass(frozen=True)
class LocalAnswer:
    ok: bool
    text: str
    reason: str       # "ok", or why the answer was refused (shown under More -> Usage; never a server message)
    ms: int
    fault: bool = True   # False when the model did nothing wrong (a call opened): it doesn't count toward the breaker


def fingerprint(model_id: str | None, path: Path | None) -> str:
    """Who the router lab passed: 12 hex digits of the model id, the file's name (it carries the quantisation) and its
    size. Not a hash of the multi-GB file (that would take a minute): the file is pinned by sha256 when it is
    installed (voicellm.json). '' when there is no such file. More -> Usage shows it; routing.yaml
    local.lab_model_hash holds the one the lab passed."""
    try:
        if not model_id or path is None or not Path(path).is_file():
            return ""
        raw = f"{model_id}|{Path(path).name}|{Path(path).stat().st_size}"
    except OSError:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


Check = Callable[[str, Any], "str | None"]       # (the prompt, the parsed answer) -> why it is refused, or None

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def strict_json(text: str) -> Any:
    """The answer as JSON, only when the whole text is JSON (alone or in one code fence). A small model that talks
    around its JSON has drifted; Claude gets the job then. ValueError otherwise."""
    s = text.strip()
    fence = _FENCE.fullmatch(s)
    return json.loads(fence.group(1) if fence else s)


# ---- the answer checks, one per purpose that may run locally (router.LOCAL_ELIGIBLE; a test keeps the two equal)

_UNTRUSTED_START, _UNTRUSTED_END = "<<<UNTRUSTED DATA", "<<<END UNTRUSTED DATA>>>"
VERDICTS = ("yes", "no", "unsure")


def asked_ids(prompt: str) -> list[str] | None:
    """The ids of the items the prompt asks about: the "id" of each object in its one untrusted block (tools.untrusted
    writes a JSON list there; JSON puts no raw line break inside a string, so no mail can fake a marker line). None
    when the prompt has no such block: then the answer cannot be checked."""
    lines = prompt.split("\n")
    starts = [i for i, ln in enumerate(lines) if ln.startswith(_UNTRUSTED_START)]
    ends = [i for i, ln in enumerate(lines) if ln.startswith(_UNTRUSTED_END)]
    if len(starts) != 1 or len(ends) != 1 or ends[0] <= starts[0]:
        return None
    try:
        items = json.loads("\n".join(lines[starts[0] + 1:ends[0]]))
    except ValueError:
        return None
    if not isinstance(items, list) or not items or not all(isinstance(i, dict) and isinstance(i.get("id"), str) for i in items):
        return None
    return [i["id"] for i in items]


def check_verdicts(prompt: str, value: Any) -> str | None:
    """reply_check: one JSON object, every asked id exactly once, each mapped to "yes", "no" or "unsure". The reasons
    say what is wrong and never quote the mail."""
    ids = asked_ids(prompt)
    if not ids:
        return "the prompt has no list of mails to check the answer against (which mails were asked about is unknown)"
    if not isinstance(value, dict):
        return "not a JSON object of verdicts"
    asked = set(ids)
    if set(value) - asked:
        return "an answer for an id that was not asked"
    if asked - set(value):
        return f"missing {len(asked - set(value))} of {len(asked)} ids"
    if not all(isinstance(v, str) and v.lower() in VERDICTS for v in value.values()):
        return "a verdict that is not yes, no or unsure"
    return None


ANSWER_CHECKS: dict[str, Check] = {"reply_check": check_verdicts}


class LocalLLM:
    def __init__(self, model, identify: Callable[[], str] | None = None) -> None:
        """model: brain.voice_local.VoiceModel (anything with ready() and an async stream(messages, ...)).
        identify: fingerprint() of the model that is running now (main.py knows which file that is)."""
        self.model = model
        self._identify = identify

    def fingerprint(self) -> str:
        try:
            return (self._identify() if self._identify else "") or ""
        except Exception:
            return ""

    def job_slot(self) -> int | None:
        """The slot of the voice model's server that only jobs use, None when it has no such slot (or doesn't know slots)."""
        try:
            slot = getattr(self.model, "job_slot", None)
        except Exception:
            return None
        return slot if isinstance(slot, int) and not isinstance(slot, bool) else None

    def unavailable(self) -> str:
        """'' when a job can run on the local model now, else why not, in words (More -> Usage and the route log show it)."""
        try:
            up = bool(self.model.ready())
        except Exception:
            up = False
        if not up:
            return "the local model is not running"
        return "" if self.job_slot() is not None else NO_JOB_SLOT

    def ready(self) -> bool:
        return not self.unavailable()

    def ask(self, prompt: str, system: str, *, timeout_s: float, check: Check, call_open: Callable[[], bool] | None = None,
            max_tokens: int = MAX_TOKENS) -> LocalAnswer:
        """check(prompt, answer) -> why the parsed answer is refused, or None. There is no way to ask without one: a
        local answer that nothing looked at is the failure mode this route must not have.

        The request goes to the job slot, never the mouth's. call_open is looked at while the model writes: when a call
        opens the ask is dropped (the connection closes, the server stops) and the job goes to Claude."""
        started = time.monotonic()

        def done(ok: bool, text: str, reason: str, fault: bool = True) -> LocalAnswer:
            return LocalAnswer(ok, text, reason, int((time.monotonic() - started) * 1000), fault)

        if len(prompt) + len(system) > MAX_PROMPT_CHARS:
            return done(False, "", "prompt too long for the local model")
        slot = self.job_slot()
        if slot is None:
            return done(False, "", NO_JOB_SLOT, fault=False)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        try:
            text, finish = _run(self._collect_until(messages, max_tokens, slot, timeout_s, call_open))
        except _CallOpened:
            return done(False, "", CALL_OPENED, fault=False)
        except asyncio.TimeoutError:
            return done(False, "", f"no answer within {timeout_s:g} s")
        except Exception as exc:       # the server's own message is not kept: it may quote the prompt
            return done(False, "", f"{type(exc).__name__} from the local model")
        if finish == "length":
            return done(False, "", "cut off at the length limit")
        if not text.strip():
            return done(False, "", "empty answer")
        try:
            value = strict_json(text)
        except ValueError:
            return done(False, "", "not valid JSON")
        if not isinstance(value, (dict, list)):
            return done(False, "", "not valid JSON")
        try:
            why = check(prompt, value)
        except Exception as exc:       # a check that breaks is a refusal, never an accepted answer
            why = f"the answer check failed ({type(exc).__name__})"
        if why:
            return done(False, "", f"answer refused: {why}")
        return done(True, text, "ok")

    async def _collect_until(self, messages: list[dict], max_tokens: int, slot: int, timeout_s: float,
                             call_open: Callable[[], bool] | None) -> tuple[str, str | None]:
        """_collect, but stopped when the time is up (TimeoutError) or a call opens (_CallOpened). Stopping cancels the
        read, which closes the connection to the server: it stops writing."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout_s
        task = asyncio.ensure_future(self._collect(messages, max_tokens, slot))
        try:
            while True:
                left = end - loop.time()
                if left <= 0:
                    raise asyncio.TimeoutError
                finished, _ = await asyncio.wait({task}, timeout=min(CALL_POLL_S, left))
                if finished:
                    return task.result()
                if call_open is not None and _call_is_open(call_open):
                    raise _CallOpened
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _collect(self, messages: list[dict], max_tokens: int, slot: int) -> tuple[str, str | None]:
        parts: list[str] = []
        finish = None
        async for ev in self.model.stream(messages, temperature=0.0, max_tokens=max_tokens, slot=slot):
            if ev.get("kind") == "text":
                parts.append(ev.get("text") or "")
            elif ev.get("kind") == "done":
                finish = ev.get("finish")
        return "".join(parts), finish


class _CallOpened(Exception):
    """A voice line opened while a job was being answered."""


def _call_is_open(call_open: Callable[[], bool]) -> bool:
    try:
        return bool(call_open())
    except Exception:         # cannot tell: the voice goes first
        return True


def _run(coro):
    """Run a coroutine to its end from sync code: a job runs in a worker thread (no loop there), but a caller
    inside a running loop gets a helper thread instead of an error."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: dict = {}

    def go() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:      # noqa: BLE001 - handed back to the caller's thread
            box["error"] = exc

    t = threading.Thread(target=go, name="local-llm", daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]
