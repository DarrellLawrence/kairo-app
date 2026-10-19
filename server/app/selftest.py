"""Kairo proves it has hands (fix P0, 2026-10-14). Run after every install/update and at boot.

Everything goes the way an owner message goes (same system prompt, same .mcp.json, same tool
list, origin owner_chat): on 2026-10-14 an older self-test (a bare "reply OK" with no tools) was
green while the owner's chat had no tools, no context and an old personality. Checks:
1. The PreToolUse hook, exactly as .claude/settings.json starts it: a forbidden tool must exit
   2 (fail closed), a harmless Kairo tool must exit 0.
2. The CLI's system/init: Kairo's MCP server "connected", and every Kairo tool listed.
3. A real task: "add a task ... due tomorrow" through the owner-chat path must arrive at the
   backend and be in the database (then it is removed again).
4. The system prompt carries the vault's Profile and Goals, and personality.md is not older
   than the release's.
(Voice: a real transcription; browser: a real page. See main.py.)

Failure categories decide what the installer does:
  toolchain  hook, MCP server or tools broken            -> roll back the update, push, red chip
  claude     logged out / usage limit / CLI down         -> push, red chip (an older version can't fix it)
  model      wired, but the model didn't add the task    -> push, red chip
  context    Profile/Goals notes exist but don't reach the model -> push, red chip
             (an empty vault on a new install is not broken: a yellow "no context yet" only)
  config     personality.md older than the release       -> push, red chip
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .vault import CONTEXT_FOLDERS

TOOL = "tasks_create"          # tier 1, the thing the owner asked for and didn't get
SAFE_TOOL = "vault_read"       # the hook check's harmless call (tier 0, reads only)
TOOL_ARGS = {"path": ""}
DENY_INPUT = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "whoami"}}
HOOK_TIMEOUT_S = 60
VERSION = re.compile(r"<!--\s*kairo-personality v(\d+)\s*-->")


def task_title(token: str) -> str:
    return f"Kairo self-test {token}"


def prompt(token: str, due_iso: str) -> str:
    """Like the owner typing "add task test tomorrow". The [[fake-tool]] line drives the CI fake CLI."""
    args = {"title": task_title(token), "due": due_iso}
    return (f"Add a task \"{task_title(token)}\" due tomorrow ({due_iso}). It's Kairo's own self-test: just do it "
            f"with your task tool, then reply DONE.\n[[fake-tool {TOOL} {json.dumps(args)}]]")


def personality_version(text: str) -> int:
    m = VERSION.search(text or "")
    return int(m.group(1)) if m else 0


def personality_version_line(text: str) -> str:
    m = VERSION.search(text or "")
    return m.group(0) if m else ""


def _has_notes(folder: Path) -> bool:
    if not folder.is_dir():
        return False
    return any(p.is_file() and "chats" not in p.relative_to(folder).parts
               and p.read_text(encoding="utf-8", errors="replace").strip() for p in folder.rglob("*.md"))


def context_checks(system_prompt: str, effective_config: Path, release_config: Path, vault_dir: Path) -> dict:
    """{profile, goals, state: ok|empty|missing, personality: current|older|unknown, detail, warning}.
    empty = the vault has no Profile/Goals notes yet (a new install, not broken: yellow);
    missing = it has them but they don't reach the model (broken: red)."""
    found = {name: f"## {folder}/" in system_prompt for name, folder in zip(("Profile", "Goals"), CONTEXT_FOLDERS)}
    has = {name: _has_notes(Path(vault_dir) / folder) for name, folder in zip(("Profile", "Goals"), CONTEXT_FOLDERS)}
    lost = [n for n in found if not found[n] and has[n]]
    empty = [n for n in found if not found[n] and not has[n]]
    detail, warning = [], ""
    if lost:
        detail.append(f"the vault has {' and '.join(lost)} notes, but they don't reach the model")
    state = "missing" if lost else ("empty" if empty else "ok")
    if empty and not lost:
        warning = (f"No context yet ({' and '.join(empty)} empty): do the onboarding (Today -> Get to know you) "
                   "so Kairo knows you")
    try:
        mine = personality_version((Path(effective_config) / "personality.md").read_text(encoding="utf-8"))
        release = personality_version((Path(release_config) / "personality.md").read_text(encoding="utf-8"))
    except OSError as exc:
        mine, release = 0, 0
        detail.append(f"personality.md: {exc}")
    personality = "unknown" if not release else ("older" if mine < release else "current")
    if personality == "older":
        detail.append(f"personality.md is v{mine}, this release has v{release}: carrying your edits into the new "
                      "version didn't work (see kairo.log)")
    return {"profile": found["Profile"], "goals": found["Goals"], "state": state, "personality": personality,
            "detail": "; ".join(detail), "warning": warning}


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
    allow_input = {"hook_event_name": "PreToolUse", "tool_name": f"mcp__kairo__{SAFE_TOOL}", "tool_input": TOOL_ARGS}
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


def verdict(*, hooks: dict, mcp: str, listed: list[str], expected: list[str], tool_used: bool, tool_arrived: bool,
            task_found: bool, claude_error: str | None, claude_status: str, context: dict) -> dict:
    """Put the checks together: ok, category, reason, checks."""
    missing = sorted(set(expected) - set(listed))
    checks = {"hook_deny": hooks["deny"], "hook_allow": hooks["allow"], "mcp": mcp, "tools_missing": missing,
              "tool_call": tool_arrived, "task_added": task_found, "claude": claude_status,
              "profile": context["profile"], "goals": context["goals"], "context": context["state"],
              "personality": context["personality"]}
    warnings = [context["warning"]] if context.get("warning") else []

    def fail(category: str, reason: str) -> dict:
        return {"ok": False, "category": category, "reason": reason, "checks": checks, "warnings": warnings}

    if not hooks["deny"] or not hooks["allow"]:
        return fail("toolchain", "the safety hook doesn't work: " + hooks["detail"])
    if claude_error:
        return fail("claude", f"Claude: {claude_status} ({claude_error[:160]})")
    if mcp != "connected":
        return fail("toolchain", f"Kairo's tool server is not connected ({mcp}): the model has no tools")
    if missing:
        if not any(t.startswith("mcp__kairo__") for t in listed):
            return fail("toolchain", "the model has no Kairo tools (the CLI listed none)")
        return fail("toolchain", f"the model is missing {len(missing)} Kairo tools: {', '.join(missing[:5])}")
    if tool_used and not tool_arrived:
        return fail("toolchain", "a tool call never reached Kairo")
    if tool_arrived and not task_found:
        return fail("toolchain", "the task tool ran but no task was saved")
    if not tool_arrived:
        return fail("model", "the model didn't add the test task although its tools work (it asked or refused instead of acting)")
    if context["state"] == "missing":
        return fail("context", context["detail"])
    if context["personality"] == "older":
        return fail("config", context["detail"])
    return {"ok": True, "category": None, "reason": "", "checks": checks, "warnings": warnings}


def add_extras(result: dict, extras: dict) -> list[str]:
    """The voice, browser, memory and GPU checks on top of the verdict. A broken install outranks the rest
    (the installer rolls back for it); the GPU (owner's request 2026-10-19: every voice part and the
    embeddings on the GPU, else red and pushed) comes last and never rolls back: an older version can't
    fix a driver or a card either. Returns the problems, in order."""
    problems = [] if result["ok"] else [result["reason"]]
    for kind, r in extras.items():
        if r.get("ok") is not False:
            continue
        problems.append(f"{kind}: {r.get('reason')}")
        if kind == "gpu":
            if result["ok"]:
                result.update(ok=False, category="gpu", reason=f"gpu: {r.get('reason')}")
        elif result["ok"] or result["category"] in ("gpu", "context", "config", "model"):
            result.update(ok=False, category=kind, reason=f"{kind}: {r.get('reason')}")
    return problems
