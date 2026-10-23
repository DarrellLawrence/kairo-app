"""The router's local route (G11): one small ask on the voice model's llama-server, for background jobs only.

OFF everywhere until the owner turns g11.local_route on and the router lab has passed a purpose (app/router.py).

This file has no network code of its own. It reaches the model only through brain/voice_local.VoiceModel.stream(),
which talks to 127.0.0.1 and nothing else, so it is not in outbound.MODULES. It cannot be given tools or images
(a job with either never goes local, see router.decide), and it accepts an answer only when it is complete and valid
JSON. Anything else is refused with a reason and the job goes to Claude as before (Router.escalate).
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass

from ..jobkit import extract_json

MAX_PROMPT_CHARS = 12_000       # about 3-4k tokens: the voice model's server runs a small context (8192 by default)
MAX_TOKENS = 700


@dataclass(frozen=True)
class LocalAnswer:
    ok: bool
    text: str
    reason: str       # "ok", or why the answer was refused (shown under More -> Usage; never a server message)
    ms: int


class LocalLLM:
    def __init__(self, model) -> None:
        """model: brain.voice_local.VoiceModel (anything with ready() and an async stream(messages, ...))."""
        self.model = model

    def ready(self) -> bool:
        try:
            return bool(self.model.ready())
        except Exception:
            return False

    def ask(self, prompt: str, system: str, *, timeout_s: float, max_tokens: int = MAX_TOKENS) -> LocalAnswer:
        started = time.monotonic()

        def done(ok: bool, text: str, reason: str) -> LocalAnswer:
            return LocalAnswer(ok, text, reason, int((time.monotonic() - started) * 1000))

        if len(prompt) + len(system) > MAX_PROMPT_CHARS:
            return done(False, "", "prompt too long for the local model")
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        try:
            text, finish = _run(asyncio.wait_for(self._collect(messages, max_tokens), timeout_s))
        except asyncio.TimeoutError:
            return done(False, "", f"no answer within {timeout_s:g} s")
        except Exception as exc:       # the server's own message is not kept: it may quote the prompt
            return done(False, "", f"{type(exc).__name__} from the local model")
        if finish == "length":
            return done(False, "", "cut off at the length limit")
        if not text.strip():
            return done(False, "", "empty answer")
        try:
            value = extract_json(text)
        except ValueError:
            return done(False, "", "not valid JSON")
        if not isinstance(value, (dict, list)):
            return done(False, "", "not valid JSON")
        return done(True, text, "ok")

    async def _collect(self, messages: list[dict], max_tokens: int) -> tuple[str, str | None]:
        parts: list[str] = []
        finish = None
        async for ev in self.model.stream(messages, temperature=0.0, max_tokens=max_tokens):
            if ev.get("kind") == "text":
                parts.append(ev.get("text") or "")
            elif ev.get("kind") == "done":
                finish = ev.get("finish")
        return "".join(parts), finish


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
