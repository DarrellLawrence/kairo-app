"""Tests against the REAL Claude Code CLI (uses a few tiny model calls).

Skipped unless KAIRO_REAL_CLI=1 and `claude` is logged in. Run before every
release that touches the hook, the workspace config or the brain adapter:

    KAIRO_REAL_CLI=1 python -m pytest tests/test_realcli.py -o addopts="" -v

Hook tests (KICKOFF 8, (e) and (f)): a probe MCP tool writes a marker file
when it really runs. Each "blocked" case also checks that the model DID try
the tool, so a model that simply didn't call it can't make a test pass.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from app.workspace import HOOK_SCRIPT, settings_json
from tests.conftest import SERVER_DIR, alembic_config

CLAUDE = shutil.which("claude")
pytestmark = pytest.mark.skipif(os.environ.get("KAIRO_REAL_CLI") != "1" or not CLAUDE,
                                reason="real Claude CLI tests: set KAIRO_REAL_CLI=1")
PROBE = SERVER_DIR / "tests" / "fakes" / "probe_mcp.py"
PROMPT = "Call the touch tool exactly once, with no arguments. Then reply with the single word DONE."


def run_probe(tmp_path: Path, hook: dict | None, extra_env: dict | None = None) -> tuple[bool, bool, str]:
    """Returns (tool_ran, model_tried_tool, transcript)."""
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    settings: dict = {"permissions": {"defaultMode": "dontAsk"}}
    if hook is not None:
        settings["hooks"] = {"PreToolUse": [{"matcher": "*", "hooks": [hook]}]}
    (ws / ".claude" / "settings.json").write_text(json.dumps(settings))
    marker = tmp_path / "ran.txt"
    mcp = tmp_path / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"probe": {"type": "stdio", "command": sys.executable, "args": [str(PROBE)],
                                                         "env": {"PROBE_MARKER": str(marker)}}}}))
    cmd = [CLAUDE, "-p", "--output-format", "stream-json", "--verbose", "--tools", "", "--strict-mcp-config",
           "--mcp-config", str(mcp), "--setting-sources", "project", "--permission-prompts", "none",
           "--allowedTools", "mcp__probe__touch", "--no-session-persistence"]
    env = {**os.environ, "DISABLE_UPDATES": "1", **(extra_env or {})}
    out = subprocess.run(cmd, input=PROMPT, cwd=ws, env=env, capture_output=True, text=True, timeout=240)
    tried = '"name":"mcp__probe__touch"' in out.stdout.replace(" ", "")
    return marker.exists(), tried, out.stdout[-3000:] + out.stderr[-1000:]


def test_positive_control_tool_runs_when_hook_says_ok(tmp_path):
    ok_hook = {"type": "command", "command": sys.executable, "args": ["-c", "import sys; sys.exit(0)"], "timeout": 20, "onFailure": "block"}
    ran, tried, log = run_probe(tmp_path, ok_hook)
    assert tried, log
    assert ran, log


def test_e_hook_that_cannot_start_blocks(tmp_path):
    missing = {"type": "command", "command": str(tmp_path / "no-such-python"), "args": ["hook.py"], "timeout": 20, "onFailure": "block"}
    ran, tried, log = run_probe(tmp_path, missing)
    assert tried, log
    assert not ran, log


def test_e_control_without_onfailure_a_missing_hook_lets_the_tool_run(tmp_path):
    """Why onFailure: block is mandatory: without it a broken hook is ignored."""
    missing = {"type": "command", "command": str(tmp_path / "no-such-python"), "args": ["hook.py"], "timeout": 20}
    ran, tried, log = run_probe(tmp_path, missing)
    assert tried, log
    assert ran, log


def test_f_hanging_hook_blocks(tmp_path):
    hang = {"type": "command", "command": sys.executable, "args": ["-c", "import time; time.sleep(60)"], "timeout": 3, "onFailure": "block"}
    ran, tried, log = run_probe(tmp_path, hang)
    assert tried, log
    assert not ran, log


def test_kairo_hook_blocks_a_tool_that_is_not_kairos(tmp_path, migrated):
    hook = settings_json()["hooks"]["PreToolUse"][0]["hooks"][0]
    env = {"KAIRO_HOOK_CONFIG_DIR": str(SERVER_DIR.parent / "config"), "KAIRO_HOOK_DB_URL": migrated.db_url,
           "KAIRO_HOOK_ORIGIN": "owner_chat"}
    ran, tried, log = run_probe(tmp_path, hook, env)
    assert tried, log
    assert not ran, log
    assert Path(hook["args"][0]) == HOOK_SCRIPT


# ---------------------------------------------------------------- whole stack: chat -> CLI -> mcp_kairo -> gate

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def chat(port: int, text: str) -> list[dict]:
    import websockets.sync.client as ws_client

    frames = []
    with ws_client.connect(f"ws://127.0.0.1:{port}/ws/chat", open_timeout=10) as ws:
        ws.send(json.dumps({"type": "message", "text": text}))
        while True:
            f = json.loads(ws.recv(timeout=240))
            frames.append(f)
            if f["type"] in ("done", "error"):
                return frames


@pytest.fixture
def server(tmp_path, migrated):
    port = free_port()
    env_file = tmp_path / "kairo.env"
    env_file.write_text(f"KAIRO_DATA_DIR={migrated.data_dir}\nKAIRO_DB_URL={migrated.db_url}\nKAIRO_CLAUDE_BIN={CLAUDE}\nKAIRO_PORT={port}\n")
    proc = subprocess.Popen([sys.executable, "-m", "app.serve", "--env-file", str(env_file)], cwd=SERVER_DIR,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            break
        except OSError:
            time.sleep(0.25)
    yield port, migrated
    proc.terminate()
    proc.wait(timeout=10)


def get(port, path):
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10).read())


def test_chat_uses_kairo_tools_through_the_gate(server):
    port, _ = server
    frames = chat(port, "Create a task titled 'realcli probe task' using your task tool, then reply DONE.")
    assert frames[-1]["type"] == "done", frames[-1]
    activity = get(port, "/api/activity")
    assert any(a["action"] == "tasks.create" and a["outcome"] == "ok" for a in activity), activity


def test_chat_send_request_becomes_a_card_not_an_email(server):
    port, _ = server
    frames = chat(port, "Send an email to test@example.com with subject 'Hi' and body 'Hello from Kairo'. Use propose_action.")
    assert frames[-1]["type"] == "done", frames[-1]
    cards = get(port, "/api/approvals")
    assert len(cards) == 1 and cards[0]["action"] == "gmail.send" and cards[0]["status"] == "open", cards
    assert cards[0]["args"]["to"] == ["test@example.com"]
