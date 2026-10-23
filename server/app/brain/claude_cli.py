"""Claude Code CLI adapter: runs the official `claude` binary as a subprocess.

Flags verified against Claude Code 2.1.296 (`claude --help` and real runs):
- `--system-prompt-file <file>`: the system prompt goes in a file, not argv
  (no Windows command-line quoting, no length limit). Works on 2.1.296
  though `--help` only mentions it in passing.
- `-p` with the prompt on **stdin**, never as an argument. A prompt starting
  with "-" would otherwise be parsed as a CLI flag, and variadic flags such as
  --allowedTools would swallow it.
- `--output-format stream-json` requires `--verbose` in print mode.
- `--resume <id>` continues a session; the session id stays the same.
- `--tools ""` removes every built-in tool (Bash, file edit, web, ...).
- `--strict-mcp-config --mcp-config <file>`: only Kairo's MCP servers.
- `--setting-sources project`: only the workspace's .claude/settings.json
  (where the PreToolUse gate hook is registered), never user settings.
- `--permission-prompts none`: anything that would prompt is denied.
- `--include-partial-messages` (fix plan 26, verified with 2.1.299): text also
  arrives as stream_event content_block_delta/text_delta while the model writes;
  the full assistant message follows, so its text is not sent twice.
- `--input-format stream-json` keeps the process alive for one turn per stdin
  line (fix plan 30, verified with 2.1.299). The CLI reads --system-prompt-file
  once, at the first turn: a warm process only serves turns that can keep the
  system prompt, which is why only calls use one (chat rebuilds it per message).

- Control requests on stdin of a stream-json process (verified with 2.1.300, real turns):
  {"type":"control_request","request_id":..,"request":{"subtype":"set_model","model":"sonnet"}}
  -> control_response success; the next turns run on that model, same process and conversation.
  {"subtype":"interrupt"} -> the running turn ends at once with result subtype
  error_during_execution; the process keeps serving turns (voice V1: barge-in, model per turn).

Stream events seen: system/init (session_id), stream_event (event.delta),
assistant (message.content), rate_limit_event (rate_limit_info.status), result
(is_error, result, api_error_status, session_id).

The end of an answer (voice G1, measured with 2.1.296): stream_event message_delta carries
delta.stop_reason and message_stop closes the message. A tool turn has one message_stop per model
message, only the last has stop_reason end_turn: that one is EventKind.message_end. The result
comes later: the CLI first writes a summary of the turn (system/post_turn_summary, 1.9-3.4 s on
Haiku). usage carries the times (ms since the turn began): first_text_ms, last_text_ms,
message_end_ms, summary_ms (when the summary event came), result_ms, and tail_ms = result_ms -
message_end_ms.
"""

from __future__ import annotations

import base64

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterator

from .base import BrainEvent, BrainStatus, EventKind

# Only these variables reach the CLI process. The backend's own secrets (for
# example the write-scoped Google token) must never be visible to the model's
# process, so the environment is built from an allow-list, not inherited.
ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "CLAUDE_CONFIG_DIR",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    # Windows: programs break without these (no SYSTEMROOT = no networking).
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "USERNAME",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)
# Always set for the CLI: Kairo's installer decides the CLI version.
FIXED_ENV = {"DISABLE_UPDATES": "1", "DISABLE_AUTOUPDATER": "1"}
ENV_PREFIX_ALLOWLIST = ("KAIRO_HOOK_",)  # settings the gate hook needs

# A warm process (fix plan 30) is closed after this long without a turn, and is not used for
# a turn when it started longer ago than WARM_MAX_AGE_S (its system prompt, e.g. "Right now", is stale).
WARM_IDLE_S = 180
WARM_MAX_AGE_S = 900

_LOGGED_OUT_MARKERS = ("/login", "not logged in", "invalid api key", "authentication", "oauth token")
_USAGE_MARKERS = ("usage limit", "rate limit", "limit reached", "out of credits")


def build_env(source: dict[str, str] | None = None) -> dict[str, str]:
    source = dict(os.environ if source is None else source)
    # Windows env names are case-insensitive (SystemRoot vs SYSTEMROOT).
    env = {k: v for k, v in source.items() if k.upper() in ENV_ALLOWLIST}
    env.update({k: v for k, v in source.items() if k.upper().startswith(ENV_PREFIX_ALLOWLIST)})
    env.update(FIXED_ENV)
    return env


def classify_error(text: str, api_status: Any) -> BrainStatus:
    lowered = (text or "").lower()
    if api_status in (401, 403) or any(m in lowered for m in _LOGGED_OUT_MARKERS):
        return BrainStatus.logged_out
    if api_status == 429 or any(m in lowered for m in _USAGE_MARKERS):
        return BrainStatus.usage_limited
    return BrainStatus.unavailable


class ClaudeCLIBrain:
    name = "claude_cli"

    def __init__(
        self,
        *,
        claude_bin: str,
        workspace_dir: Path,
        mcp_config: Path | None = None,
        timeout_s: int = 300,
        model: str | None = None,
        env: dict[str, str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self.claude_bin = claude_bin
        self.workspace_dir = Path(workspace_dir)
        self.mcp_config = mcp_config
        self.timeout_s = timeout_s
        self.model = model
        self._env = build_env(env)
        # Settings the gate hook needs (KAIRO_HOOK_*), set by the backend.
        self._env.update(extra_env or {})
        self._status = BrainStatus.ok
        self._run_limited = False
        self.mcp_status: dict[str, str] = {}   # from the CLI's system/init event: server -> connected / failed / ...
        self._proc: subprocess.Popen | None = None
        self._cancelled = threading.Event()
        self._warm: _Warm | None = None
        self._warm_lock = threading.Lock()
        self._streamed = False                 # this message's text already went out as deltas
        self._stop_reason: str | None = None   # of the message being streamed (message_delta), read at message_stop
        self._t0 = 0.0                         # when this run (or warm turn) started
        self._timing: dict[str, Any] = {}
        self._turn_warm: _Warm | None = None     # the warm process answering right now (interrupt)
        self._interrupted = threading.Event()   # the owner talked over the answer: not a failure

    # -- command ---------------------------------------------------------

    def build_command(self, *, session_id: str | None, system: str, tools: list[str], images: bool = False,
                      model: str | None = None, prompt_file: str = "system-prompt.md") -> list[str]:
        cmd = [
            *self._launcher(),
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "project",
            "--permission-prompts", "none",
            "--system-prompt-file", str(self._system_prompt_file(system, prompt_file)),
        ]
        if self.mcp_config is not None and tools:
            # Fix plan 27: a run without tools (jobs) doesn't start Kairo's MCP server at all.
            cmd += ["--mcp-config", str(self.mcp_config)]
        if tools:
            # One comma-separated argument, so the variadic flag can't swallow anything else.
            cmd += ["--allowedTools", ",".join(tools)]
        if model or self.model:
            cmd += ["--model", model or self.model]   # alias: haiku / sonnet / opus (verified with CLI 2.1.299)
        if session_id:
            cmd += ["--resume", session_id]
        if images:
            # JSON user messages on stdin, one per line (text + base64 images; or a warm process's
            # turns), verified with CLI 2.1.299.
            cmd += ["--input-format", "stream-json"]
        return cmd

    def _launcher(self) -> list[str]:
        # A .py "binary" is the fake CLI used in tests; Windows can't exec it directly.
        if self.claude_bin.endswith(".py"):
            return [sys.executable, self.claude_bin]
        return [self.claude_bin]

    def _system_prompt_file(self, system: str, name: str = "system-prompt.md") -> Path:
        # One brain call at a time (BrainRunner), so one file is enough; the warm process has its own.
        path = self.workspace_dir / ".kairo" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(system, encoding="utf-8")
        return path

    def cancel(self) -> None:
        """Stop the running call (kill switch, or a job making way for the owner). Safe when idle."""
        self._cancelled.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.kill()

    # -- warm process (fix plan 30) ----------------------------------------

    def warm_up(self, *, key: Any, session_id: str | None, system: str, tools: list[str], model: str | None = None,
                origin: str = "owner_chat", idle_s: float = None, max_age_s: float = None) -> bool:
        """Start the CLI for the next turn now, so that turn doesn't wait for it to start (a call).
        It waits on stdin; nothing reaches the model until run(warm=key) sends the turn. Returns
        False when a matching warm process is already there."""
        with self._warm_lock:
            w = self._warm
            if w is not None and (w.busy or w.usable(key, session_id)):
                return False               # never cut off a turn that is running
            self._close_warm_locked()
            cmd = self.build_command(session_id=session_id, system=system, tools=tools, images=True, model=model,
                                     prompt_file="system-prompt-warm.md")
            self.workspace_dir.mkdir(parents=True, exist_ok=True)
            try:
                proc = subprocess.Popen(cmd, cwd=self.workspace_dir, env=self.hook_env(origin), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            except OSError:
                return False
            self._warm = _Warm(proc, key, session_id, on_idle=self._close_if_idle, model=model or self.model,
                               idle_s=idle_s or WARM_IDLE_S, max_age_s=max_age_s or WARM_MAX_AGE_S)
            return True

    def warm_ready(self, key: Any, session_id: str | None) -> bool:
        with self._warm_lock:
            return self._warm is not None and self._warm.usable(key, session_id)

    def close_warm(self) -> None:
        with self._warm_lock:
            self._close_warm_locked()

    def _close_warm_locked(self) -> None:
        w, self._warm = self._warm, None
        if w is not None:
            w.close()

    def _close_if_idle(self, w: "_Warm") -> None:
        with self._warm_lock:
            if self._warm is w and not w.busy:
                self._close_warm_locked()

    def _take_warm(self, key: Any, session_id: str | None) -> "_Warm | None":
        with self._warm_lock:
            w = self._warm
            if w is not None and w.proc.poll() is not None:
                self._close_warm_locked()      # died while waiting
                return None
            if w is None or not w.usable(key, session_id):
                return None
            w.busy = True
            return w

    # -- Brain protocol --------------------------------------------------

    def health(self) -> BrainStatus:
        """Cheap check: binary present + last known run status. Never spends usage."""
        if shutil.which(self.claude_bin) is None and not Path(self.claude_bin).is_file():
            return BrainStatus.unavailable
        return self._status

    def hook_env(self, origin: str) -> dict[str, str]:
        """The environment the CLI (and so the PreToolUse hook it starts) runs with."""
        return {**self._env, "KAIRO_HOOK_ORIGIN": origin}

    def tools_status(self) -> str:
        """Did the CLI's last run connect Kairo's MCP server? (system/init mcp_servers)"""
        if self.mcp_config is None:
            return "none"
        if not self.mcp_status:
            return "unknown"
        bad = [f"{n}: {st}" for n, st in sorted(self.mcp_status.items()) if st != "connected"]
        return "connected" if not bad else "failed (" + ", ".join(bad) + ")"

    def run(
        self,
        prompt: str,
        *,
        session_id: str | None,
        system: str,
        tools: list[str],
        origin: str = "owner_chat",
        images: list[tuple[bytes, str]] | None = None,
        model: str | None = None,
        warm: Any = None,
    ) -> Iterator[BrainEvent]:
        """warm: the key of a warm process (warm_up) that may serve this turn. If there is none, or it
        died before answering, the turn runs the normal way (a new process)."""
        if warm is not None:
            w = self._take_warm(warm, session_id)
            if w is not None:
                done = yield from self._run_warm(w, prompt, images, model)
                if done:
                    return
        if session_id is not None:
            # This turn continues the conversation without the warm process: the warm one's
            # copy of it would be one turn behind, so it goes.
            with self._warm_lock:
                if self._warm is not None and self._warm.session_id == session_id:
                    self._close_warm_locked()
        yield from self._run_cold(prompt, session_id=session_id, system=system, tools=tools, origin=origin,
                                  images=images, model=model)

    def interrupt(self) -> bool:
        """The owner talked over the answer: stop the running turn now. A warm process keeps running
        (control request interrupt); a cold one is stopped. False when nothing was running."""
        w = self._turn_warm
        if w is not None:
            self._interrupted.set()
            return w.control({"subtype": "interrupt"}, wait=False)
        proc = self._proc
        if proc is not None and proc.poll() is None:
            self._interrupted.set()
            proc.kill()
            return True
        return False

    def _start_turn(self) -> None:
        self._run_limited = False
        self._cancelled.clear()
        self._interrupted.clear()
        self._streamed = False
        self._stop_reason = None
        self._t0 = time.monotonic()
        self._timing = {}

    def _run_cold(self, prompt: str, *, session_id, system, tools, origin, images, model) -> Iterator[BrainEvent]:
        cmd = self.build_command(session_id=session_id, system=system, tools=tools, images=bool(images), model=model)
        if images:
            prompt = _user_message(prompt, images)
        run_env = self.hook_env(origin)
        self._start_turn()
        self._timing["warm"] = False
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=self.workspace_dir,
                env=run_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
        except OSError as exc:
            self._status = BrainStatus.unavailable
            yield BrainEvent(EventKind.error, text=f"Could not start the Claude CLI: {exc}", status=self._status)
            return

        # Remember that WE killed it: on Windows a killed process exits with 1,
        # not a negative signal number, so the exit code can't tell.
        timed_out = threading.Event()

        def on_timeout() -> None:
            timed_out.set()
            proc.kill()

        self._proc = proc
        timer = threading.Timer(self.timeout_s, on_timeout)
        timer.start()
        stderr_chunks: list[str] = []
        stderr_reader = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True)
        stderr_reader.start()
        finished = False
        try:
            assert proc.stdin is not None and proc.stdout is not None
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except BrokenPipeError:
                pass
            for line in proc.stdout:
                for event in self._parse_line(line):
                    if event.kind in (EventKind.result, EventKind.error):
                        finished = True
                    yield event
            proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            self._proc = None
        stderr_reader.join(timeout=5)
        if not finished:
            stderr = "".join(stderr_chunks).strip()
            if self._interrupted.is_set():
                yield BrainEvent(EventKind.error, text="Interrupted.", status=self._status,
                                 data={"cancelled": True, "interrupted": True})
                return
            if self._cancelled.is_set():
                yield BrainEvent(EventKind.error, text="Stopped by the kill switch.", status=self._status, data={"cancelled": True})
                return
            msg = "The Claude CLI timed out." if timed_out.is_set() else (stderr or f"The Claude CLI exited with code {proc.returncode}.")
            self._status = classify_error(msg, None)
            yield BrainEvent(EventKind.error, text=msg, status=self._status)

    def _run_warm(self, w: "_Warm", prompt: str, images, model: str | None = None) -> Iterator[BrainEvent]:
        """One turn on the warm process. Returns True when the turn is done (answered or failed after
        it started), False when the process was already gone: then the caller runs it cold.
        model: switched first when the process runs another one (control request set_model)."""
        self._start_turn()
        self._timing["warm"] = True
        proc = w.proc
        if model and w.model != model:
            if not w.control({"subtype": "set_model", "model": model}):
                with self._warm_lock:
                    w.busy = False
                    if self._warm is w:
                        self._close_warm_locked()
                return False      # no answer to the switch: run it cold, on the right model
            w.model = model
        timed_out = threading.Event()

        def on_timeout() -> None:
            timed_out.set()
            proc.kill()

        self._proc = proc
        self._turn_warm = w
        timer = threading.Timer(self.timeout_s, on_timeout)
        finished = started = False
        try:
            if not w.write(_user_message(prompt, images or [])):
                return False
            timer.start()
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                for event in self._parse_line(line):
                    started = True
                    if event.kind is EventKind.session and event.session_id:
                        w.session_id = event.session_id
                    if event.kind in (EventKind.result, EventKind.error):
                        finished = True
                    yield event
                if finished:
                    break
        finally:
            timer.cancel()
            self._proc = None
            self._turn_warm = None
            with self._warm_lock:
                w.busy = False
                if not finished or proc.poll() is not None:
                    if self._warm is w:
                        self._close_warm_locked()
                    else:
                        w.close()
                else:
                    w.touch()
        if finished:
            return True
        if not started and not self._cancelled.is_set() and not timed_out.is_set():
            return False      # gone before it answered: run it cold
        if self._cancelled.is_set():
            yield BrainEvent(EventKind.error, text="Stopped by the kill switch.", status=self._status, data={"cancelled": True})
            return True
        stderr = "".join(w.stderr).strip()
        msg = "The Claude CLI timed out." if timed_out.is_set() else (stderr or "The Claude CLI stopped.")
        self._status = classify_error(msg, None)
        yield BrainEvent(EventKind.error, text=msg, status=self._status)
        return True

    # -- parsing ---------------------------------------------------------

    def _parse_line(self, line: str) -> Iterator[BrainEvent]:
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return  # stray non-JSON output; ignore
        kind = msg.get("type")
        if kind == "system" and msg.get("subtype") == "init":
            self._timing.setdefault("startup_ms", self._ms())
            servers = [x for x in (msg.get("mcp_servers") or []) if isinstance(x, dict)]
            if servers:
                self.mcp_status = {str(x.get("name")): str(x.get("status")) for x in servers}
            yield BrainEvent(EventKind.session, session_id=msg.get("session_id"),
                             data={"mcp_servers": servers, "tools": msg.get("tools") or []})
        elif kind == "system" and msg.get("subtype") == "post_turn_summary":
            self._timing.setdefault("summary_ms", self._ms())       # what the tail after the answer is made of
        elif kind == "stream_event":
            ev = msg.get("event") or {}
            if ev.get("type") == "message_start":
                self._streamed = False
                self._stop_reason = None
            elif ev.get("type") == "message_delta":
                self._stop_reason = (ev.get("delta") or {}).get("stop_reason")
            elif ev.get("type") == "message_stop":
                reason, self._stop_reason = self._stop_reason, None
                if reason == "end_turn":                  # a tool call's message ends with tool_use: not the end
                    now = self._timing["message_end_ms"] = self._ms()
                    yield BrainEvent(EventKind.message_end, data={"ms": now})
            elif ev.get("type") == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                text = ev["delta"].get("text") or ""
                if text:
                    self._streamed = True
                    self._timing.setdefault("first_text_ms", self._ms())
                    self._timing["last_text_ms"] = self._ms()
                    yield BrainEvent(EventKind.text, text=text)
        elif kind == "assistant":
            for block in (msg.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    if self._streamed:
                        continue          # already sent, delta by delta
                    self._timing.setdefault("first_text_ms", self._ms())
                    self._timing["last_text_ms"] = self._ms()
                    yield BrainEvent(EventKind.text, text=block["text"])
                elif block.get("type") == "tool_use":
                    yield BrainEvent(EventKind.tool_use, data={"name": block.get("name"), "input": block.get("input")})
        elif kind == "rate_limit_event":
            info = msg.get("rate_limit_info") or {}
            if info.get("status") not in (None, "allowed", "allowed_warning"):
                self._run_limited = True
                self._status = BrainStatus.usage_limited
            yield BrainEvent(EventKind.usage, data=info)
        elif kind == "result":
            sid = msg.get("session_id")
            self._timing["result_ms"] = self._ms()
            if "message_end_ms" in self._timing:
                self._timing["tail_ms"] = self._timing["result_ms"] - self._timing["message_end_ms"]
            usage = {**_usage(msg), **self._timing}
            if self._interrupted.is_set() and msg.get("subtype") == "error_during_execution":
                yield BrainEvent(EventKind.error, text="Interrupted.", session_id=sid, status=self._status,
                                 data={"usage": usage, "cancelled": True, "interrupted": True})
            elif msg.get("is_error") or msg.get("subtype") != "success":
                text = str(msg.get("result") or msg.get("subtype") or "error")
                self._status = classify_error(text, msg.get("api_error_status"))
                yield BrainEvent(EventKind.error, text=text, session_id=sid, status=self._status, data={"usage": usage})
            else:
                self._status = BrainStatus.usage_limited if self._run_limited else BrainStatus.ok
                yield BrainEvent(EventKind.result, text=str(msg.get("result") or ""), session_id=sid, status=self._status,
                                 data={"usage": usage})


    def _ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)


class _Warm:
    """A CLI process waiting for its next turn on stdin (fix plan 30). Voice V1: the conversation
    process for chat and calls lives long (idle_s, max_age_s) and runs the model each turn asks for."""

    def __init__(self, proc: subprocess.Popen, key: Any, session_id: str | None, *, on_idle, model: str | None = None,
                 idle_s: float = WARM_IDLE_S, max_age_s: float = WARM_MAX_AGE_S) -> None:
        self.proc, self.key, self.session_id = proc, key, session_id
        self.model = model
        self.idle_s, self.max_age_s = idle_s, max_age_s
        self.started = time.monotonic()
        self.busy = False
        self.stderr: deque[str] = deque(maxlen=40)
        self._on_idle = on_idle
        self._idle: threading.Timer | None = None
        self._stdin = threading.Lock()
        self._seq = 0
        threading.Thread(target=self._drain, name="brain-warm-stderr", daemon=True).start()
        self.touch()

    def write(self, line: str) -> bool:
        with self._stdin:
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    def control(self, request: dict, *, wait: bool = True, timeout: float = 5.0) -> bool:
        """Send a control request. wait: read stdout until its answer (only between turns, when
        nothing else is written there); a running turn's reader skips control responses itself."""
        self._seq += 1
        rid = f"kairo-{self._seq}"
        if not self.write(json.dumps({"type": "control_request", "request_id": rid, "request": request}) + "\n"):
            return False
        if not wait:
            return True
        result: list[bool] = []

        def read() -> None:
            try:
                for line in self.proc.stdout:
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    resp = msg.get("response") or {} if msg.get("type") == "control_response" else {}
                    if resp.get("request_id") == rid:
                        result.append(resp.get("subtype") == "success")
                        return
            except (OSError, ValueError):
                return

        reader = threading.Thread(target=read, name="brain-warm-control", daemon=True)
        reader.start()
        reader.join(timeout)
        return bool(result and result[0])

    def _drain(self) -> None:
        try:
            for line in self.proc.stderr:
                self.stderr.append(line)
        except (OSError, ValueError):
            pass

    def touch(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
        self._idle = threading.Timer(self.idle_s, self._on_idle, args=(self,))
        self._idle.daemon = True
        self._idle.start()

    def usable(self, key: Any, session_id: str | None) -> bool:
        return (not self.busy and self.key == key and self.session_id == session_id and self.proc.poll() is None
                and time.monotonic() - self.started < self.max_age_s)

    def close(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
        proc = self.proc

        def stop() -> None:   # stdin closed = the CLI ends by itself (and stops its MCP server); else kill
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

        threading.Thread(target=stop, name="brain-warm-close", daemon=True).start()


def _user_message(prompt: str, images: list[tuple[bytes, str]]) -> str:
    """One stream-json user message line: the images (or PDFs) first, then the text."""
    content = [{"type": "document" if mt == "application/pdf" else "image",
                "source": {"type": "base64", "media_type": mt, "data": base64.b64encode(data).decode()}}
               for data, mt in images]
    return json.dumps({"type": "user", "message": {"role": "user", "content": content + [{"type": "text", "text": prompt}]}}) + "\n"


def _usage(msg: dict) -> dict:
    """Which model ran and what it took, from the CLI's result line (modelUsage per model id)."""
    per_model = msg.get("modelUsage") or {}
    main = max(per_model.items(), key=lambda kv: (kv[1] or {}).get("outputTokens", 0), default=(None, {}))
    totals = {"input_tokens": 0, "output_tokens": 0}
    for u in per_model.values():
        u = u or {}
        totals["input_tokens"] += int(u.get("inputTokens") or 0) + int(u.get("cacheReadInputTokens") or 0) + int(u.get("cacheCreationInputTokens") or 0)
        totals["output_tokens"] += int(u.get("outputTokens") or 0)
    return {"model": main[0], "duration_ms": msg.get("duration_ms"), "cost_usd": msg.get("total_cost_usd"), **totals}
