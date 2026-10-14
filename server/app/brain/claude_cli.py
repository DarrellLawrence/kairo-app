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

Stream events seen: system/init (session_id), assistant (message.content),
rate_limit_event (rate_limit_info.status), result (is_error, result,
api_error_status, session_id).
"""

from __future__ import annotations

import base64

import json
import os
import shutil
import subprocess
import sys
import threading
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
        self._proc: subprocess.Popen | None = None
        self._cancelled = threading.Event()

    # -- command ---------------------------------------------------------

    def build_command(self, *, session_id: str | None, system: str, tools: list[str], images: bool = False,
                      model: str | None = None) -> list[str]:
        cmd = [
            *self._launcher(),
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "project",
            "--permission-prompts", "none",
            "--system-prompt-file", str(self._system_prompt_file(system)),
        ]
        if self.mcp_config is not None:
            cmd += ["--mcp-config", str(self.mcp_config)]
        if tools:
            # One comma-separated argument, so the variadic flag can't swallow anything else.
            cmd += ["--allowedTools", ",".join(tools)]
        if model or self.model:
            cmd += ["--model", model or self.model]   # alias: haiku / sonnet / opus (verified with CLI 2.1.299)
        if session_id:
            cmd += ["--resume", session_id]
        if images:
            # One JSON user message on stdin (text + base64 images), verified with CLI 2.1.299.
            cmd += ["--input-format", "stream-json"]
        return cmd

    def _launcher(self) -> list[str]:
        # A .py "binary" is the fake CLI used in tests; Windows can't exec it directly.
        if self.claude_bin.endswith(".py"):
            return [sys.executable, self.claude_bin]
        return [self.claude_bin]

    def _system_prompt_file(self, system: str) -> Path:
        # One brain call at a time (BrainRunner), so one file is enough.
        path = self.workspace_dir / ".kairo" / "system-prompt.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(system, encoding="utf-8")
        return path

    def cancel(self) -> None:
        """Stop the running call (kill switch). Safe to call when idle."""
        self._cancelled.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.kill()

    # -- Brain protocol --------------------------------------------------

    def health(self) -> BrainStatus:
        """Cheap check: binary present + last known run status. Never spends usage."""
        if shutil.which(self.claude_bin) is None and not Path(self.claude_bin).is_file():
            return BrainStatus.unavailable
        return self._status

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
    ) -> Iterator[BrainEvent]:
        cmd = self.build_command(session_id=session_id, system=system, tools=tools, images=bool(images), model=model)
        if images:
            content = [{"type": "document" if mt == "application/pdf" else "image",
                        "source": {"type": "base64", "media_type": mt, "data": base64.b64encode(data).decode()}}
                       for data, mt in images]
            prompt = json.dumps({"type": "user", "message": {"role": "user", "content": content + [{"type": "text", "text": prompt}]}}) + "\n"
        run_env = {**self._env, "KAIRO_HOOK_ORIGIN": origin}
        self._run_limited = False
        self._cancelled.clear()
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
            if self._cancelled.is_set():
                yield BrainEvent(EventKind.error, text="Stopped by the kill switch.", status=self._status, data={"cancelled": True})
                return
            msg = "The Claude CLI timed out." if timed_out.is_set() else (stderr or f"The Claude CLI exited with code {proc.returncode}.")
            self._status = classify_error(msg, None)
            yield BrainEvent(EventKind.error, text=msg, status=self._status)

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
            yield BrainEvent(EventKind.session, session_id=msg.get("session_id"))
        elif kind == "assistant":
            for block in (msg.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text"):
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
            usage = _usage(msg)
            if msg.get("is_error") or msg.get("subtype") != "success":
                text = str(msg.get("result") or msg.get("subtype") or "error")
                self._status = classify_error(text, msg.get("api_error_status"))
                yield BrainEvent(EventKind.error, text=text, session_id=sid, status=self._status, data={"usage": usage})
            else:
                self._status = BrainStatus.usage_limited if self._run_limited else BrainStatus.ok
                yield BrainEvent(EventKind.result, text=str(msg.get("result") or ""), session_id=sid, status=self._status,
                                 data={"usage": usage})


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
