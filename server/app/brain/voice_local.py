"""The voice model (owner's decision 2026-10-16): a small LLM on the PC that only talks.

It runs in llama.cpp's server on 127.0.0.1 (never another host), loaded day and night, so a turn never
waits for a model to start. Its one tool is ask_kairo: anything real (notes, tasks, mail, calendar,
approvals) goes to the Claude brain, which answers as today. No thinking mode: every token is speech.

This module keeps the chosen model's server running (restarts it when it dies, gives it up for the
voice lab) and streams one turn from it. The conversation's start stays the same all call long, so
llama.cpp's prompt cache only has to read the newest words.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import AsyncIterator, Callable

import httpx

log = logging.getLogger("kairo.voice_llm")

HOST = "127.0.0.1"
PORT = int(os.environ.get("KAIRO_VOICE_LLM_PORT", "8091"))
START_TIMEOUT_S = 180          # a 9B model from a cold disk
RETRY_AFTER_S = 300            # a server that failed to start is tried again after this
CHECK_EVERY_S = 15

ASK_KAIRO = {
    "type": "function",
    "function": {
        "name": "ask_kairo",
        "description": ("Hand the owner's question or request to Kairo's brain, which has their notes, tasks, mail, "
                        "calendar and memory, and prepares anything for other people as an approval card. Use it for "
                        "everything you don't know from the call context, and to pass on the owner's answer to a "
                        "bring-up item. It returns what to say."),
        "parameters": {
            "type": "object",
            "properties": {
                "request": {"type": "string", "description": "What the owner wants or said, close to their own words."},
                "item": {"type": "string", "description": "The bring-up item this answers (its id), if any."},
            },
            "required": ["request"],
        },
    },
}


def server_args(model: Path, *, port: int = PORT, context: int = 8192) -> list[str]:
    """Flags checked against llama.cpp's own argument list (common/arg.cpp, Oct 2026)."""
    return ["-m", str(model), "--host", HOST, "--port", str(port), "-c", str(context),
            "-ngl", "all",                  # every layer in VRAM
            "-fa", "auto",                  # flash attention where the GPU has it
            "--jinja",                      # the model's own chat template: its tool-call format
            "--reasoning", "off", "--reasoning-budget", "0",   # no thinking: straight to words
            "-np", "1",                     # one conversation: one slot, its cache kept
            "--cache-reuse", "256",         # reuse the cached start even when later text shifts
            "--no-webui"]


class LlamaServer:
    """One llama-server process. command_prefix: [binary] (tests use [python, fake_llama.py])."""

    def __init__(self, command_prefix: list[str], model: Path, *, port: int = PORT, context: int = 8192,
                 log_path: Path | None = None, pid_path: Path | None = None):
        self.cmd = [*command_prefix, *server_args(model, port=port, context=context)]
        self.model, self.port, self.log_path, self.pid_path = Path(model), port, log_path, pid_path
        self.proc: subprocess.Popen | None = None
        self.started_at: float | None = None

    def start(self) -> None:
        _kill_stale(self.pid_path, self.port)
        out = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL   # noqa: SIM115 (kept for the process)
        flags = 0x08000000 if sys.platform == "win32" else 0                       # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(self.cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     creationflags=flags)
        self.started_at = time.monotonic()
        if self.pid_path:
            self.pid_path.parent.mkdir(parents=True, exist_ok=True)
            self.pid_path.write_text(str(self.proc.pid), encoding="ascii")

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout: float = 10) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self.proc = None
        if self.pid_path:
            self.pid_path.unlink(missing_ok=True)


def _kill_stale(pid_path: Path | None, port: int) -> None:
    """A server left behind by a Kairo that crashed still holds the port and the VRAM. Only when a server
    still answers on the voice model's port: after a reboot the old pid can belong to another program."""
    if not pid_path or not pid_path.is_file():
        return
    try:
        pid = int(pid_path.read_text(encoding="ascii").strip())
        if pid == os.getpid() or not health(port, timeout=1.0):
            raise ValueError("not ours")
        os.kill(pid, signal.SIGTERM)                  # TerminateProcess on Windows
        log.info("voice model: stopped a server left from before (pid %s)", pid)
    except (ValueError, OSError):
        pass
    pid_path.unlink(missing_ok=True)


def health(port: int = PORT, timeout: float = 2.0) -> bool:
    try:
        return httpx.get(f"http://{HOST}:{port}/health", timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False


class VoiceModel:
    """Keeps the active voice model loaded and hands out turns.

    resolve() -> (command_prefix, model path, model id, context) or None when nothing is installed.
    state: not installed | starting | ready | failed | paused | off (installed, but no model passed the lab)."""

    def __init__(self, resolve: Callable[[], tuple[list[str], Path, str, int] | None], *, log_dir: Path,
                 port: int = PORT, transport: httpx.AsyncBaseTransport | None = None):
        self.resolve, self.log_dir, self.port, self.transport = resolve, Path(log_dir), port, transport
        self.server: LlamaServer | None = None
        self.model_id: str | None = None
        self.state, self.error, self.since = "not installed", None, time.time()
        self.failed_at = 0.0
        self.paused = False
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self.turns = 0

    # ---------------------------------------------------------------- the process

    def _set(self, state: str, error: str | None = None) -> None:
        if state != self.state or error != self.error:
            self.state, self.error, self.since = state, error, time.time()

    def ensure(self) -> str:
        """Start or restart the server when it should run and doesn't. Returns the state."""
        with self.lock:
            if self.paused:
                return self.state
            want = self.resolve()
            if want is None or isinstance(want, str):          # a str: installed, but switched off (why)
                self._stop_server()
                self._set("not installed" if want is None else "off", want)
                return self.state
            prefix, model, model_id, context = want
            if self.server is not None and (self.model_id != model_id or not self.server.alive()):
                if self.server.alive():
                    log.info("voice model: switching to %s", model_id)
                else:
                    log.warning("voice model: %s stopped; starting it again", self.model_id)
                self._stop_server()
            if self.server is None:
                if self.state == "failed" and time.time() - self.failed_at < RETRY_AFTER_S:
                    return self.state
                self.log_dir.mkdir(parents=True, exist_ok=True)
                self.server = LlamaServer(prefix, model, port=self.port, context=context,
                                          log_path=self.log_dir / "voice-llm.log", pid_path=self.log_dir / "voice-llm.pid")
                try:
                    self.server.start()
                except OSError as exc:
                    self.server = None
                    self.failed_at = time.time()
                    self._set("failed", f"could not start llama-server: {exc}")
                    return self.state
                self.model_id = model_id
                self._set("starting")
            if self.state == "starting":
                if health(self.port):
                    self._set("ready")
                    log.info("voice model %s ready in %.1fs", model_id, time.monotonic() - (self.server.started_at or 0))
                elif not self.server.alive() or time.monotonic() - (self.server.started_at or 0) > START_TIMEOUT_S:
                    tail = _tail(self.log_dir / "voice-llm.log")
                    self._stop_server()
                    self.failed_at = time.time()
                    self._set("failed", f"llama-server did not start ({tail})")
            elif self.state == "ready" and not health(self.port, timeout=5):
                self._set("starting")          # busy or restarting; checked again next round
            return self.state

    def wait_ready(self, timeout: float = START_TIMEOUT_S) -> bool:
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if self.ensure() == "ready":
                return True
            if self.state in ("failed", "not installed", "paused", "off"):
                return False
            time.sleep(0.5)
        return False

    def _stop_server(self) -> None:
        if self.server is not None:
            self.server.stop()
            self.server = None
            self.model_id = None

    def watch(self) -> threading.Thread:
        """Loaded day and night: checks the server every 15 s."""
        def loop():
            while not self._stop.is_set():
                try:
                    self.ensure()
                except Exception:   # the watcher never dies
                    log.exception("voice model watcher")
                self._stop.wait(CHECK_EVERY_S if self.state != "starting" else 1)
        t = threading.Thread(target=loop, name="voice-llm", daemon=True)
        t.start()
        return t

    def shutdown(self) -> None:
        self._stop.set()
        with self.lock:
            self._stop_server()
            self._set("stopped")

    def pause(self) -> None:
        """The voice lab takes the GPU: the old voice pipeline answers meanwhile."""
        with self.lock:
            self.paused = True
            self._stop_server()
            self._set("paused")

    def resume(self) -> None:
        with self.lock:
            self.paused = False
            self._set("not installed")
        self.ensure()

    def ready(self) -> bool:
        return self.state == "ready" and not self.paused

    def status(self) -> dict:
        return {"state": self.state, "model": self.model_id, "error": self.error, "since": self.since,
                "port": self.port, "turns": self.turns}

    # ---------------------------------------------------------------- one turn

    async def stream(self, messages: list[dict], *, tools: list[dict] | None = None, temperature: float = 0.6,
                     max_tokens: int = 300) -> AsyncIterator[dict]:
        """Events: {"kind": "text", "text"} as it is written; {"kind": "tool_call", "id", "name", "arguments"}
        once a call is complete; {"kind": "done", "finish", "first_token_ms", "timings", "usage"} at the end.
        Closing the generator (talked over) closes the connection, and the server stops writing."""
        self.turns += 1
        body = {"messages": messages, "stream": True, "temperature": temperature, "max_tokens": max_tokens,
                "cache_prompt": True, "stream_options": {"include_usage": True},
                "chat_template_kwargs": {"enable_thinking": False}}
        if tools:
            body.update(tools=tools, tool_choice="auto", parallel_tool_calls=False)
        started = time.monotonic()
        first = None
        calls: dict[int, dict] = {}
        finish, timings, usage = None, None, None
        async with httpx.AsyncClient(base_url=f"http://{HOST}:{self.port}", transport=self.transport,
                                     timeout=httpx.Timeout(30.0, connect=2.0)) as c:
            async with c.stream("POST", "/v1/chat/completions", json=body) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"voice model said {r.status_code}: {(await r.aread())[:200]!r}")
                async for line in r.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    try:
                        chunk = json.loads(line[6:])
                    except ValueError:
                        continue
                    timings = chunk.get("timings") or timings
                    usage = chunk.get("usage") or usage
                    for ch in chunk.get("choices") or []:
                        delta = ch.get("delta") or {}
                        if delta.get("content"):
                            if first is None:
                                first = int((time.monotonic() - started) * 1000)
                            yield {"kind": "text", "text": delta["content"]}
                        for tc in delta.get("tool_calls") or []:
                            if first is None:
                                first = int((time.monotonic() - started) * 1000)
                            slot = calls.setdefault(int(tc.get("index", 0)), {"id": None, "name": "", "arguments": ""})
                            slot["id"] = tc.get("id") or slot["id"]
                            fn = tc.get("function") or {}
                            slot["name"] += fn.get("name") or ""
                            slot["arguments"] += fn.get("arguments") or ""
                            if fn.get("name"):
                                yield {"kind": "tool_start", "name": slot["name"]}
                        finish = ch.get("finish_reason") or finish
        for i in sorted(calls):
            call = calls[i]
            yield {"kind": "tool_call", "id": call["id"] or f"call_{i}", "name": call["name"], "arguments": call["arguments"]}
        yield {"kind": "done", "finish": finish, "first_token_ms": first, "timings": timings or {}, "usage": usage or {}}


def _tail(path: Path, n: int = 300) -> str:
    try:
        data = path.read_bytes()[-4000:].decode("utf-8", errors="replace").strip().splitlines()
        return (" | ".join(data[-3:]))[-n:] or "no output"
    except OSError:
        return "no log"
