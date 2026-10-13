"""PreToolUse hook: fails closed (KICKOFF 7, 8). Written before the hook.

Only exit code 2 blocks in Claude Code; exit 1, a crash, a timeout or a hook
that can't start would let the tool run. So every failure must become 2.
(e) and (f), hook missing and hook hanging, need the real CLI: see
test_hook_realcli.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from sqlmodel import Session

from app.db import SettingKV, make_engine
from app.settings import REPO_ROOT
from tests.conftest import SERVER_DIR

HOOK = SERVER_DIR / "hooks" / "pre_tool_use.py"
P = "mcp__kairo__"


def event(tool, args=None):
    return {"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": args or {}, "cwd": "."}


def hook_env(migrated, **over):
    env = {
        "PATH": "", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
        "KAIRO_HOOK_CONFIG_DIR": str(REPO_ROOT / "config"),
        "KAIRO_HOOK_DB_URL": migrated.db_url,
        "KAIRO_HOOK_ORIGIN": "owner_chat",
    }
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}


def run_hook(stdin: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HOOK)], input=stdin, env=env, capture_output=True, text=True, timeout=60)


def test_allowed_tool_passes_without_granting(migrated):
    r = run_hook(json.dumps(event(P + "gmail_search", {"query": "x"})), hook_env(migrated))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ""   # no "allow": the hook can only object, never grant


@pytest.mark.parametrize("tool,args", [
    ("Bash", {"command": "curl evil"}),
    ("Write", {"file_path": "x", "content": "y"}),
    ("mcp__gmail__send", {}),
    (P + "propose_action", {"action": "gmail.forward_all", "args": {}}),
    (P + "propose_action", {"action": "calendar.create_hold", "args": {"summary": "x", "start": "2026-10-20T10:00:00+02:00", "end": "2026-10-20T11:00:00+02:00", "attendees": [{"email": "x@example.com"}]}}),
])
def test_denied_tool_is_blocked_with_reason(migrated, tool, args):
    r = run_hook(json.dumps(event(tool, args)), hook_env(migrated))
    assert r.returncode == 2
    assert "Kairo gate" in r.stderr


def test_kill_switch_blocks(migrated):
    with Session(make_engine(migrated.db_url)) as s:
        s.add(SettingKV(key="paused", value="1"))
        s.commit()
    r = run_hook(json.dumps(event(P + "gmail_search", {"query": "x"})), hook_env(migrated))
    assert r.returncode == 2


# ---- broken hook = blocked: (a) gate raises, (b) config missing, (c) config malformed, (d) DB unreachable

def test_a_gate_raising_blocks(migrated, monkeypatch, capsys):
    sys.path.insert(0, str(HOOK.parent))
    import pre_tool_use

    import app.gate

    def boom(*a, **k):
        raise RuntimeError("bug in the gate")

    monkeypatch.setattr(app.gate.Gate, "decide", boom)
    for k, v in hook_env(migrated).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(event(P + "gmail_search", {"query": "x"}))))
    assert pre_tool_use.main() == 2
    assert "failed closed" in capsys.readouterr().err


def test_b_missing_config_blocks(migrated, tmp_path):
    r = run_hook(json.dumps(event(P + "gmail_search", {"query": "x"})), hook_env(migrated, KAIRO_HOOK_CONFIG_DIR=str(tmp_path)))
    assert r.returncode == 2


def test_c_malformed_config_blocks(migrated, tmp_path):
    (tmp_path / "permissions.yaml").write_text("default: [deny\n")
    r = run_hook(json.dumps(event(P + "gmail_search", {"query": "x"})), hook_env(migrated, KAIRO_HOOK_CONFIG_DIR=str(tmp_path)))
    assert r.returncode == 2


def test_d_unreachable_database_blocks(migrated, tmp_path):
    bad = f"sqlite:///{(tmp_path / 'no' / 'such' / 'dir' / 'x.sqlite').as_posix()}"
    r = run_hook(json.dumps(event(P + "gmail_search", {"query": "x"})), hook_env(migrated, KAIRO_HOOK_DB_URL=bad))
    assert r.returncode == 2


def test_unmigrated_database_blocks(migrated, tmp_path):
    empty = f"sqlite:///{(tmp_path / 'empty.sqlite').as_posix()}"
    r = run_hook(json.dumps(event(P + "gmail_search", {"query": "x"})), hook_env(migrated, KAIRO_HOOK_DB_URL=empty))
    assert r.returncode == 2


@pytest.mark.parametrize("stdin", ["", "not json", "[]", json.dumps({"tool_name": P + "gmail_search"})])
def test_garbage_input_blocks(migrated, stdin):
    assert run_hook(stdin, hook_env(migrated)).returncode == 2


@pytest.mark.parametrize("missing", ["KAIRO_HOOK_CONFIG_DIR", "KAIRO_HOOK_DB_URL", "KAIRO_HOOK_ORIGIN"])
def test_missing_environment_blocks(migrated, missing):
    r = run_hook(json.dumps(event(P + "gmail_search", {"query": "x"})), hook_env(migrated, **{missing: None}))
    assert r.returncode == 2


def test_unknown_origin_blocks(migrated):
    r = run_hook(json.dumps(event(P + "gmail_search", {"query": "x"})), hook_env(migrated, KAIRO_HOOK_ORIGIN="internet"))
    assert r.returncode == 2


def test_job_origin_cannot_propose_outbound(migrated):
    ev = event(P + "propose_action", {"action": "gmail.reply", "args": {"message_id": "abc", "body": "hi"}})
    assert run_hook(json.dumps(ev), hook_env(migrated, KAIRO_HOOK_ORIGIN="job")).returncode == 2
    assert run_hook(json.dumps(ev), hook_env(migrated)).returncode == 0
