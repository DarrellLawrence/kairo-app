"""Claude Code CLI adapter: runs the official `claude` binary as a subprocess.

Flags verified against Claude Code 2.1.296 (`claude --help` and real runs):
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

import json
import os
import shutil
import subprocess
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
)
ENV_PREFIX_ALLOWLIST = ("KAIRO_HOOK_",)  # settings the gate hook needs

_LOGGED_OUT_MARKERS = ("/login", "not logged in", "invalid api key", "authentication", "oauth token")
_USAGE_MARKERS = ("usage limit", "rate limit", "limit reached", "out of credits")


def build_env(source: dict[str, str] | None = None) -> dict[str, str]:
    source = dict(os.environ if source is None else source)
    env = {k: v for k, v in source.items() if k in ENV_ALLOWLIST}
    env.update({k: v for k, v in source.items() if k.startswith(ENV_PREFIX_ALLOWLIST)})
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
    ) -> None:
        self.claude_bin = claude_bin
        self.workspace_dir = Path(workspace_dir)
        self.mcp_config = mcp_config
        self.timeout_s = timeout_s
        self.model = model
        self._env = build_env(env)
        self._status = BrainStatus.ok
        self._run_limited = False

    # -- command ---------------------------------------------------------

    def build_command(self, *, session_id: str | None, system: str, tools: list[str]) -> list[str]:
        cmd = [
            self.claude_bin,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "project",
            "--permission-prompts", "none",
            "--system-prompt", system,
        ]
        if self.mcp_config is not None:
            cmd += ["--mcp-config", str(self.mcp_config)]
        if tools:
            # One comma-separated argument, so the variadic flag can't swallow anything else.
            cmd += ["--allowedTools", ",".join(tools)]
        if self.model:
            cmd += ["--model", self.model]
        if session_id:
            cmd += ["--resume", session_id]
        return cmd

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
    ) -> Iterator[BrainEvent]:
        cmd = self.build_command(session_id=session_id, system=system, tools=tools)
        self._run_limited = False
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=self.workspace_dir,
                env=self._env,
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

        timer = threading.Timer(self.timeout_s, proc.kill)
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
        stderr_reader.join(timeout=5)
        if not finished:
            stderr = "".join(stderr_chunks).strip()
            timed_out = proc.returncode is not None and proc.returncode < 0
            msg = "The Claude CLI timed out." if timed_out else (stderr or f"The Claude CLI exited with code {proc.returncode}.")
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
            if msg.get("is_error") or msg.get("subtype") != "success":
                text = str(msg.get("result") or msg.get("subtype") or "error")
                self._status = classify_error(text, msg.get("api_error_status"))
                yield BrainEvent(EventKind.error, text=text, session_id=sid, status=self._status)
            else:
                self._status = BrainStatus.usage_limited if self._run_limited else BrainStatus.ok
                yield BrainEvent(EventKind.result, text=str(msg.get("result") or ""), session_id=sid, status=self._status)
