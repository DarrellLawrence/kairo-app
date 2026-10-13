"""Writes the CLI workspace config at every backend start (KICKOFF 7).

<workspace>/.claude/settings.json
    PreToolUse hook for every tool: exec form (this Python + the hook script,
    no shell, same on Windows and Linux), onFailure: block, short timeout.
    permissions.deny for every built-in tool as a backstop to --tools "".
<workspace>/.mcp.json
    mcp_kairo only, pointed at this backend with a per-start secret.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
HOOK_SCRIPT = SERVER_DIR / "hooks" / "pre_tool_use.py"
HOOK_TIMEOUT_S = 30

BUILTIN_TOOLS = [
    "Bash", "PowerShell", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep", "LS",
    "WebFetch", "WebSearch", "Task", "Agent", "TodoWrite", "Monitor", "Skill", "SlashCommand",
]


def settings_json(python: str = sys.executable, hook: Path = HOOK_SCRIPT, timeout: int = HOOK_TIMEOUT_S) -> dict:
    return {
        "hooks": {
            "PreToolUse": [{
                "matcher": "*",
                "hooks": [{"type": "command", "command": python, "args": [str(hook)],
                           "timeout": timeout, "onFailure": "block"}],
            }],
        },
        "permissions": {"deny": BUILTIN_TOOLS, "defaultMode": "dontAsk"},
        "env": {"DISABLE_UPDATES": "1", "DISABLE_AUTOUPDATER": "1"},
    }


def mcp_json(tool_url: str, token: str, python: str = sys.executable) -> dict:
    return {"mcpServers": {"kairo": {
        "type": "stdio",
        "command": python,
        "args": ["-m", "mcp_kairo"],
        "env": {"PYTHONPATH": str(SERVER_DIR), "KAIRO_TOOL_URL": tool_url, "KAIRO_TOOL_TOKEN": token},
    }}}


def write_workspace(workspace: Path, *, tool_url: str, token: str) -> Path:
    workspace = Path(workspace)
    (workspace / ".claude").mkdir(parents=True, exist_ok=True)
    (workspace / ".claude" / "settings.json").write_text(json.dumps(settings_json(), indent=2), encoding="utf-8")
    mcp = workspace / ".mcp.json"
    mcp.write_text(json.dumps(mcp_json(tool_url, token), indent=2), encoding="utf-8")
    return mcp
