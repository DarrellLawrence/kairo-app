"""Kairo proves it has hands (fix P0): the self-test the installer runs after every install/update
and the boot check runs at boot. Here end to end with a real server, the real hook script, the
real mcp_kairo and the fake CLI (which starts MCP and hooks like the real one)."""

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

from app import selftest
from app.workspace import settings_json

SERVER_DIR = Path(__file__).resolve().parents[1]
FAKE_CLAUDE = SERVER_DIR / "tests" / "fakes" / "fake_claude.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- the verdict

HOOKS_OK = {"deny": True, "allow": True, "detail": ""}


@pytest.mark.parametrize("hooks,mcp,used,arrived,error,category", [
    (HOOKS_OK, "connected", True, True, None, None),
    ({"deny": False, "allow": True, "detail": "hook script missing"}, "connected", True, True, None, "toolchain"),
    (HOOKS_OK, "failed", False, False, None, "toolchain"),               # the real 2.1.296 init when mcp_kairo is missing
    (HOOKS_OK, "connected", True, False, None, "toolchain"),             # called, never arrived
    (HOOKS_OK, "connected", False, False, None, "model"),                # wired, the model just didn't call it
    (HOOKS_OK, "unknown", False, False, "Invalid API key · Please run /login", "claude"),
])
def test_verdict_categories(hooks, mcp, used, arrived, error, category):
    v = selftest.verdict(hooks, mcp, used, arrived, error, "logged_out" if error else "ok")
    assert v["category"] == category and v["ok"] is (category is None)


def test_hook_checks_find_a_missing_hook_script(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    settings = settings_json(hook=tmp_path / "gone" / "pre_tool_use.py")
    (ws / ".claude" / "settings.json").write_text(json.dumps(settings))
    r = selftest.hook_checks(ws, dict(os.environ))
    assert r["deny"] is False and "missing" in r["detail"]


def test_hook_checks_no_settings(tmp_path):
    assert selftest.hook_checks(tmp_path, {})["deny"] is False


# ---------------------------------------------------------------- end to end

def serve(tmp_path, migrated, extra: str = ""):
    port = free_port()
    env_file = tmp_path / "kairo.env"
    env_file.write_text(f"KAIRO_DATA_DIR={migrated.data_dir}\nKAIRO_DB_URL={migrated.db_url}\n"
                        f"KAIRO_CLAUDE_BIN={FAKE_CLAUDE}\nKAIRO_PORT={port}\nKAIRO_SCHEDULER=0\n{extra}")
    proc = subprocess.Popen([sys.executable, "-m", "app.serve", "--env-file", str(env_file)], cwd=SERVER_DIR,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(120):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            break
        except OSError:
            time.sleep(0.25)
    return proc, port


def post(port, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def test_selftest_passes_with_the_real_hook_and_mcp_kairo(tmp_path, migrated):
    proc, port = serve(tmp_path, migrated)
    try:
        r = post(port, "/api/selftest/tools?notify=0")
        assert r["ok"] is True, r
        assert r["checks"] == {"hook_deny": True, "hook_allow": True, "mcp": "connected", "tool_call": True, "claude": "ok",
                               "voice": None, "browser": None}    # not set up in dev: skipped, not failed
        health = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/health").read())
        assert health["tools"] == "connected"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_selftest_catches_a_release_without_the_tool_bridge(tmp_path, migrated):
    """What every release so far looked like: no server/hooks, no server/mcp_kairo."""
    shipped = tmp_path / "server"
    shutil.copytree(SERVER_DIR / "app", shipped / "app", ignore=shutil.ignore_patterns("__pycache__"))
    for f in ("alembic.ini",):
        shutil.copy(SERVER_DIR / f, shipped / f)
    shutil.copytree(SERVER_DIR / "migrations", shipped / "migrations", ignore=shutil.ignore_patterns("__pycache__"))
    port = free_port()
    env_file = tmp_path / "kairo.env"
    env_file.write_text(f"KAIRO_DATA_DIR={migrated.data_dir}\nKAIRO_DB_URL={migrated.db_url}\nKAIRO_CONFIG_DIR={migrated.config_dir}\n"
                        f"KAIRO_CLAUDE_BIN={FAKE_CLAUDE}\nKAIRO_PORT={port}\nKAIRO_SCHEDULER=0\n"
                        f"KAIRO_WEB_DIST={tmp_path / 'nodist'}\n")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.Popen([sys.executable, "-m", "app.serve", "--env-file", str(env_file)], cwd=shipped,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except OSError:
                time.sleep(0.25)
        r = post(port, "/api/selftest/tools?notify=0")
        assert r["ok"] is False and r["category"] == "toolchain", r
        assert r["checks"]["hook_deny"] is False
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ---------------------------------------------------------------- voice/browser packages Kairo's account can't read

def test_a_folder_without_readable_init_is_not_installed(tmp_path, monkeypatch):
    """kairo-home 2026-10-14: faster_whisper imported as an empty namespace package (unreadable folder)."""
    from app import tool_sites
    from app.voice import Voice
    (tmp_path / "faster_whisper").mkdir()            # what kairo-svc saw: a folder, no readable __init__.py
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "faster_whisper", raising=False)
    ok, detail = tool_sites.is_real_package("faster_whisper")
    assert not ok and "not readable" in detail
    assert Voice.installed()["stt"] is False
    assert any("faster_whisper" in b for b in Voice.broken())
    r = Voice(tmp_path / "models").selftest()
    assert r["ok"] is False and r["checks"]["stt_import"] is False and "WhisperModel" in r["reason"]


def test_site_comes_from_the_release(tmp_path, monkeypatch):
    from app import tool_sites
    monkeypatch.setattr(tool_sites, "REPO_ROOT", tmp_path)
    assert tool_sites.site("voice", tmp_path / "data") == tmp_path / "tools" / "voice" and not tool_sites.configured("voice")
    (tmp_path / "voice-site.txt").write_text(r"C:\Kairo\tools\voice-0123456789ab" + "\n")
    assert tool_sites.configured("voice") and str(tool_sites.site("voice", tmp_path)).endswith("voice-0123456789ab")
