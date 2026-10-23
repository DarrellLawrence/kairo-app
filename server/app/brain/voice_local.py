"""The voice model (owner's decision 2026-10-16): a small LLM on the PC that only talks.

It runs in llama.cpp's server on 127.0.0.1 (never another host), loaded day and night, so a turn never
waits for a model to start. Its one tool is ask_kairo: anything real (notes, tasks, mail, calendar,
approvals) goes to the Claude brain, which answers as today. No thinking mode: every token is speech.

This module keeps the chosen model's server running (restarts it when it dies, gives it up for the
voice lab) and streams one turn from it. The conversation's start stays the same all call long, so
llama.cpp's prompt cache only has to read the newest words.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
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


# ---------------------------------------------------------------- tool calls written as text (owner's call 2026-10-20)
# Some models write their tool calls into the words and llama-server passes them through as content:
# LFM2 `<|tool_call_start|>[ask_kairo(request="...")]<|tool_call_end|>` (Pythonic), Hermes/Qwen
# `<tool_call>{"name": ..., "arguments": {...}}</tool_call>`. After "Hello" a call once said the first one
# out loud. TextCalls takes them out of the stream; nothing of a marker is ever spoken.

_CALL_TAGS = {"<|tool_call_start|>": "<|tool_call_end|>", "<tool_call>": "</tool_call>"}
_STRAY = ("<|tool_call_end|>", "</tool_call>")
_SPECIAL = re.compile(r"<\|[^<>|\s]{1,40}\|>")            # any other special token (<|im_end|>, ...)
_MAYBE_SPECIAL = re.compile(r"<\|[^<>|\s]{0,40}\|?")      # could still become one: wait for more
_CALL_NAME = re.compile(r"""\s*\[?\s*(?:[A-Za-z_]\w*\.)?([A-Za-z_]\w*)\s*\(|\s*\[?\s*\{\s*"name"\s*:\s*"([^"]{1,80})\"""")
MAX_CALL_CHARS = 4000


def _offered(tools: list[dict] | None) -> dict[str, tuple[list[str], list[str]]]:
    """name -> (its parameters in order, the required ones), for the tools offered on this turn."""
    out = {}
    for t in tools or []:
        fn = t.get("function") or t
        params = fn.get("parameters") or {}
        if fn.get("name"):
            out[fn["name"]] = (list((params.get("properties") or {}).keys()), list(params.get("required") or []))
    return out


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\-]", "", str(name or ""))[:40] or "?"


def check_call(name: str, args, offered: dict) -> str | None:
    """None when the call may run; else why not. Valid only if the tool was offered on this turn and every
    required argument is there and not empty (ask_kairo without request text is no call)."""
    if name not in offered:
        return "not offered on this turn"
    if not isinstance(args, dict):
        return "arguments are not an object"
    for key in offered[name][1]:
        v = args.get(key)
        if v is None or (isinstance(v, str) and not v.strip()) or v in ([], {}):
            return f"no {key}"
    return None


def _parse_body(body: str, offered: dict) -> list[tuple[str, dict | None]]:
    """[(name, args or None when unreadable)] from what was between the tags. Pythonic calls are read with
    ast (literals only: ast.literal_eval, never eval); JSON with json."""
    b = body.strip()
    if not b or len(b) > MAX_CALL_CHARS:
        return [("?", None)]
    if re.match(r"\[?\s*\{", b):
        try:
            data = json.loads(b)
        except ValueError:
            return [("?", None)]
        out = []
        for d in data if isinstance(data, list) else [data]:
            if not isinstance(d, dict):
                out.append(("?", None))
                continue
            args = d.get("arguments", d.get("parameters", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except ValueError:
                    args = None
            out.append((str(d.get("name") or "?"), args if isinstance(args, dict) else None))
        return out
    try:
        tree = ast.parse(b, mode="eval").body
    except (SyntaxError, ValueError):
        return [("?", None)]
    out = []
    for node in tree.elts if isinstance(tree, (ast.List, ast.Tuple)) else [tree]:
        if not isinstance(node, ast.Call) or not isinstance(node.func, (ast.Name, ast.Attribute)):
            out.append(("?", None))
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        params = offered.get(name, ([], []))[0]
        try:
            args = {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords if kw.arg}
            if any(kw.arg is None for kw in node.keywords) or len(node.args) > len(params):
                raise ValueError("unnamed arguments")
            for key, value in zip(params, node.args):
                args.setdefault(key, ast.literal_eval(value))
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            out.append((name, None))
            continue
        out.append((name, args))
    return out


class TextCalls:
    """Takes tool calls written as text out of the streamed words. feed()/flush() give events: text (safe to
    speak: never a piece of a marker), tool_start (the call's name is known and it was offered: the holding
    line can start), tool_call (complete and valid; arguments as a JSON string, like the OpenAI-style path).
    Invalid or not-offered calls are dropped without a word: only the tool's name and the reason are logged."""

    def __init__(self, tools: list[dict] | None):
        self.offered = _offered(tools)
        self.buf = ""
        self.closer: str | None = None       # inside a call: the tag that ends it
        self.started = False                 # tool_start given for the call being written
        self.n = 0

    def feed(self, delta: str) -> list[dict]:
        self.buf += delta or ""
        out: list[dict] = []
        while self.buf:
            if self.closer is not None:
                j = self.buf.find(self.closer)
                if j < 0:
                    self._early_start(out)
                    if len(self.buf) > MAX_CALL_CHARS:          # runaway: keep only what could hold the end tag
                        self.buf = self.buf[-len(self.closer):]
                    return out
                body, self.buf = self.buf[:j], self.buf[j + len(self.closer):]
                self._early_start(out, body)
                self._finish(body, out)
                continue
            i = self.buf.find("<")
            if i < 0:
                self._text(self.buf, out)
                self.buf = ""
                return out
            if i:
                self._text(self.buf[:i], out)
                self.buf = self.buf[i:]
            rest = self.buf
            opener = next((o for o in _CALL_TAGS if rest.startswith(o)), None)
            if opener is not None:
                self.closer, self.started, self.buf = _CALL_TAGS[opener], False, rest[len(opener):]
                continue
            stray = next((s for s in _STRAY if rest.startswith(s)), None)
            if stray is not None:
                self.buf = rest[len(stray):]
                continue
            if any(t.startswith(rest) for t in (*_CALL_TAGS, *_STRAY)):
                return out                                      # could still be a tag: wait for more
            m = _SPECIAL.match(rest)
            if m:
                self.buf = rest[m.end():]                        # a special token: never spoken
                continue
            if _MAYBE_SPECIAL.fullmatch(rest):
                return out
            self._text("<", out)                                 # a plain "<" in the words
            self.buf = rest[1:]
        return out

    def flush(self) -> list[dict]:
        out = self.feed("")
        if self.closer is not None:                              # cut off before its end tag (max tokens)
            log.info("voice model: a tool call was dropped (%s: not finished)", self._name(self.buf))
        elif self.buf and not any(t.startswith(self.buf) for t in (*_CALL_TAGS, *_STRAY)) \
                and not _MAYBE_SPECIAL.fullmatch(self.buf):
            self._text(self.buf, out)
        self.buf, self.closer = "", None
        return out

    def _text(self, text: str, out: list[dict]) -> None:
        if text:
            out.append({"kind": "text", "text": text})

    def _name(self, body: str) -> str:
        m = _CALL_NAME.match(body)
        return _safe((m.group(1) or m.group(2)) if m else "?")

    def _early_start(self, out: list[dict], body: str | None = None) -> None:
        if self.started:
            return
        m = _CALL_NAME.match(self.buf if body is None else body)
        name = (m.group(1) or m.group(2)) if m else None
        if name in self.offered:
            self.started = True
            out.append({"kind": "tool_start", "name": name})

    def _finish(self, body: str, out: list[dict]) -> None:
        self.closer = None
        for name, args in _parse_body(body, self.offered):
            why = "unreadable arguments" if args is None else check_call(name, args, self.offered)
            if why:
                log.info("voice model: a tool call was dropped (%s: %s)", _safe(name), why)
                continue
            props = self.offered[name][0]
            out.append({"kind": "tool_call", "id": f"call_text{self.n}", "name": name,
                        "arguments": json.dumps({k: v for k, v in args.items() if not props or k in props},
                                                ensure_ascii=False)})
            self.n += 1


JOB_SLOT = 1            # the slot background jobs use (router local route) when the server has more than one; the mouth keeps slot 0
JOB_MIN_CONTEXT = 6144  # tokens a job's slot needs: local_llm.MAX_PROMPT_CHARS is about 4k tokens, plus its answer


def _int_flag(cmd: list[str], *names: str) -> int | None:
    """The number after the first of these flags in a llama-server command line, or None."""
    for i, arg in enumerate(cmd[:-1]):
        if arg in names:
            try:
                return int(cmd[i + 1])
            except ValueError:
                return None
    return None


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


def props(port: int = PORT, timeout: float = 2.0) -> dict | None:
    """llama-server's own settings (GET /props: context size, slots, chat template) for `kairo diag`, or None."""
    try:
        r = httpx.get(f"http://{HOST}:{port}/props", timeout=timeout)
        body = r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    return body if isinstance(body, dict) else None


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

    @property
    def job_slot(self) -> int | None:
        """The slot a background job may use without touching the mouth's cached start of its prompt, or None.
        There is one only when the running server was started with two or more slots (-np) that each have room for a job
        (llama-server splits -c between its slots). With one slot (today's flags) a job would evict the mouth's cached start and a
        call opening meanwhile would queue behind it, so the router's local route stays off (voice recipe 13)."""
        srv = self.server
        if srv is None or not self.ready():
            return None
        slots = _int_flag(srv.cmd, "-np", "--parallel") or 1
        context = _int_flag(srv.cmd, "-c", "--ctx-size") or 0
        return JOB_SLOT if slots >= 2 and context // slots >= JOB_MIN_CONTEXT else None

    def status(self) -> dict:
        return {"state": self.state, "model": self.model_id, "error": self.error, "since": self.since,
                "port": self.port, "turns": self.turns}

    # ---------------------------------------------------------------- one turn

    async def stream(self, messages: list[dict], *, tools: list[dict] | None = None, temperature: float = 0.6,
                     max_tokens: int = 300, slot: int | None = None) -> AsyncIterator[dict]:
        """Events: {"kind": "text", "text"} as it is written; {"kind": "tool_start", "name"} when an offered
        tool's call begins; {"kind": "tool_call", "id", "name", "arguments"} once a valid call is complete
        (OpenAI-style deltas or written as text, TextCalls); {"kind": "done", "finish", "first_token_ms",
        "timings", "usage"} at the end. Closing the generator (talked over) closes the connection, and the
        server stops writing. slot: pin the request to one of the server's slots (a background job passes job_slot);
        without it the mouth stays on slot 0 when the server has a slot for jobs, and the request is exactly what it
        was when the server has one."""
        self.turns += 1
        texts = TextCalls(tools)
        body = {"messages": messages, "stream": True, "temperature": temperature, "max_tokens": max_tokens,
                "cache_prompt": True, "stream_options": {"include_usage": True},
                "chat_template_kwargs": {"enable_thinking": False}}
        if tools:
            body.update(tools=tools, tool_choice="auto", parallel_tool_calls=False)
        if slot is None and self.job_slot is not None:
            slot = 0
        if slot is not None:
            body["id_slot"] = slot
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
                            for ev in texts.feed(delta["content"]):
                                yield ev
                        for tc in delta.get("tool_calls") or []:
                            if first is None:
                                first = int((time.monotonic() - started) * 1000)
                            slot = calls.setdefault(int(tc.get("index", 0)), {"id": None, "name": "", "arguments": ""})
                            slot["id"] = tc.get("id") or slot["id"]
                            fn = tc.get("function") or {}
                            slot["name"] += fn.get("name") or ""
                            slot["arguments"] += fn.get("arguments") or ""
                            if fn.get("name") and slot["name"] in texts.offered:   # no holding line for a tool it can't use
                                yield {"kind": "tool_start", "name": slot["name"]}
                        finish = ch.get("finish_reason") or finish
        for ev in texts.flush():
            yield ev
        for i in sorted(calls):
            call = calls[i]
            try:
                args = json.loads(call["arguments"] or "{}")
            except ValueError:
                args = None
            why = "unreadable arguments" if args is None else check_call(call["name"], args, texts.offered)
            if why:
                log.info("voice model: a tool call was dropped (%s: %s)", _safe(call["name"]), why)
                continue
            yield {"kind": "tool_call", "id": call["id"] or f"call_{i}", "name": call["name"], "arguments": call["arguments"]}
        yield {"kind": "done", "finish": finish, "first_token_ms": first, "timings": timings or {}, "usage": usage or {}}


def _tail(path: Path, n: int = 300) -> str:
    try:
        data = path.read_bytes()[-4000:].decode("utf-8", errors="replace").strip().splitlines()
        return (" | ".join(data[-3:]))[-n:] or "no output"
    except OSError:
        return "no log"
