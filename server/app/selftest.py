"""Kairo proves it has hands (fix P0, 2026-10-14). Run after every install/update and at boot.

Three checks, all against what is really installed:
1. The PreToolUse hook, exactly as .claude/settings.json starts it: a forbidden tool
   must exit 2 (fail closed), a harmless Kairo tool must exit 0.
2. The CLI's system/init event must report Kairo's MCP server as "connected".
3. One harmless real tool call (vault_read) must arrive at the backend through mcp_kairo.

Failure categories decide what the installer does:
  toolchain  hook or MCP broken            -> roll back the update, push, red chip
  claude     logged out / usage limit / CLI down -> no rollback (an older version can't fix it); push, red chip
  model      everything wired, but the model didn't call the tool (twice) -> warning only
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

TOOL = "vault_read"            # tier 0, in permissions.yaml since the first release, reads only
TOOL_ARGS = {"path": ""}
PROMPT = ("Kairo self-test. Call the tool mcp__kairo__vault_read once with path \"\" and then reply with "
          "exactly: SELFTEST-OK\n[[fake-tool vault_read {\"path\": \"\"}]]")
SYSTEM = "You are Kairo's self-test. Use the one tool you have exactly once, then reply SELFTEST-OK. Nothing else."
DENY_INPUT = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "whoami"}}
HOOK_TIMEOUT_S = 60


def hook_checks(workspace_dir: Path, env: dict[str, str]) -> dict:
    """Run the configured PreToolUse hook(s) like the CLI does. {'deny': bool, 'allow': bool, 'detail': str}"""
    path = Path(workspace_dir) / ".claude" / "settings.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
        hooks = [h for e in settings["hooks"]["PreToolUse"] for h in e["hooks"]]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"deny": False, "allow": False, "detail": f"no PreToolUse hook configured ({exc})"}
    if not hooks:
        return {"deny": False, "allow": False, "detail": "no PreToolUse hook configured"}
    allow_input = {"hook_event_name": "PreToolUse", "tool_name": f"mcp__kairo__{TOOL}", "tool_input": TOOL_ARGS}
    deny, allow, detail = True, True, []
    for hook in hooks:
        cmd = [hook["command"], *hook.get("args", [])]
        missing = [a for a in hook.get("args", []) if a.endswith(".py") and not Path(a).is_file()]
        if missing:
            return {"deny": False, "allow": False, "detail": f"hook script missing: {missing[0]}"}
        for name, payload, want in (("deny", DENY_INPUT, 2), ("allow", allow_input, 0)):
            try:
                r = subprocess.run(cmd, input=json.dumps(payload), capture_output=True, text=True,
                                   timeout=HOOK_TIMEOUT_S, env=env, cwd=str(workspace_dir))
                code, err = r.returncode, (r.stderr or "").strip()[:200]
            except (OSError, subprocess.TimeoutExpired) as exc:
                code, err = None, str(exc)[:200]
            if code != want:
                detail.append(f"{name}: hook exit {code}, expected {want} ({err})")
                if name == "deny":
                    deny = False
                else:
                    allow = False
    return {"deny": deny, "allow": allow, "detail": "; ".join(detail)}


def verdict(hooks: dict, mcp: str, tool_used: bool, tool_arrived: bool, claude_error: str | None,
            claude_status: str) -> dict:
    """Put the checks together: ok, category, reason."""
    checks = {"hook_deny": hooks["deny"], "hook_allow": hooks["allow"], "mcp": mcp,
              "tool_call": tool_arrived, "claude": claude_status}
    if not hooks["deny"] or not hooks["allow"]:
        return {"ok": False, "category": "toolchain", "reason": "the safety hook doesn't work: " + hooks["detail"], "checks": checks}
    if claude_error:
        return {"ok": False, "category": "claude", "reason": f"Claude: {claude_status} ({claude_error[:160]})", "checks": checks}
    if mcp != "connected":
        return {"ok": False, "category": "toolchain", "reason": f"Kairo's tool server is not connected ({mcp})", "checks": checks}
    if tool_used and not tool_arrived:
        return {"ok": False, "category": "toolchain", "reason": "a tool call never reached Kairo", "checks": checks}
    if not tool_arrived:
        return {"ok": False, "category": "model", "reason": "the model didn't use its tool (wiring is fine)", "checks": checks}
    return {"ok": True, "category": None, "reason": "", "checks": checks}
