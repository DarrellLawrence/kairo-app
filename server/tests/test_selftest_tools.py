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
CTX_OK = {"profile": True, "goals": True, "state": "ok", "personality": "current", "detail": "", "warning": ""}
EXPECTED = ["mcp__kairo__tasks_create", "mcp__kairo__vault_read"]


def v(hooks=HOOKS_OK, mcp="connected", listed=EXPECTED, used=True, arrived=True, task=True, error=None, ctx=CTX_OK):
    return selftest.verdict(hooks=hooks, mcp=mcp, listed=listed, expected=EXPECTED, tool_used=used, tool_arrived=arrived,
                            task_found=task, claude_error=error, claude_status="logged_out" if error else "ok", context=ctx)


@pytest.mark.parametrize("kw,category", [
    ({}, None),
    ({"hooks": {"deny": False, "allow": True, "detail": "hook script missing"}}, "toolchain"),
    ({"mcp": "failed", "listed": [], "used": False, "arrived": False, "task": False}, "toolchain"),   # 2026-10-14 on kairo-home
    ({"listed": [], "used": False, "arrived": False, "task": False}, "toolchain"),     # "connected" but no Kairo tools
    ({"listed": ["mcp__kairo__vault_read"]}, "toolchain"),                             # some tools missing
    ({"arrived": False, "task": False}, "toolchain"),                                 # called, never arrived
    ({"task": False}, "toolchain"),                                                   # arrived, no task in the database
    ({"used": False, "arrived": False, "task": False}, "model"),                      # wired, model didn't act ("want me to...?")
    ({"ctx": {**CTX_OK, "profile": False, "goals": False, "state": "missing", "detail": "notes don't reach"}}, "context"),
    ({"ctx": {**CTX_OK, "profile": False, "goals": False, "state": "empty", "warning": "No context yet"}}, None),   # yellow
    ({"ctx": {**CTX_OK, "personality": "older", "detail": "v3 < v6"}}, "config"),
    ({"error": "Invalid API key · Please run /login", "listed": [], "used": False, "arrived": False, "task": False}, "claude"),
])
def test_verdict_categories(kw, category):
    r = v(**kw)
    assert r["category"] == category and r["ok"] is (category is None), r


def test_hook_checks_find_a_missing_hook_script(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    settings = settings_json(hook=tmp_path / "gone" / "pre_tool_use.py")
    (ws / ".claude" / "settings.json").write_text(json.dumps(settings))
    r = selftest.hook_checks(ws, dict(os.environ))
    assert r["deny"] is False and "missing" in r["detail"]


def test_hook_checks_no_settings(tmp_path):
    assert selftest.hook_checks(tmp_path, {})["deny"] is False


def test_context_checks(tmp_path):
    from app.settings import REPO_ROOT
    cfg = REPO_ROOT / "config"
    prompt = "x\n## 10 Profile/me.md\nThe owner\n## 20 Goals/2026.md\nFinish\n"
    vault(tmp_path)
    ok = selftest.context_checks(prompt, cfg, cfg, tmp_path / "vault")
    assert ok["state"] == "ok" and ok["personality"] == "current" and not ok["warning"]
    empty = selftest.context_checks("(Empty so far. Suggest the onboarding interview", cfg, cfg, tmp_path / "nothing")
    assert empty["state"] == "empty" and "onboarding" in empty["warning"] and not empty["detail"]   # yellow, not red
    lost = selftest.context_checks("(Empty so far.", cfg, cfg, tmp_path / "vault")
    assert lost["state"] == "missing" and "don't reach the model" in lost["detail"]                  # red
    old = tmp_path / "cfg"
    old.mkdir()
    text = (cfg / "personality.md").read_text(encoding="utf-8")
    (old / "personality.md").write_text(text.replace(selftest.personality_version_line(text), "<!-- kairo-personality v1 -->"),
                                        encoding="utf-8")
    assert selftest.context_checks(prompt, old, cfg, tmp_path / "vault")["personality"] == "older"


def test_personality_version_counts_every_change():
    """Change personality.md -> bump its version line and run scripts/config_history.py."""
    from app import config_layers as cl
    from app.settings import REPO_ROOT
    text = (REPO_ROOT / "config" / "personality.md").read_text(encoding="utf-8")
    history = json.loads((REPO_ROOT / "config" / cl.HISTORY).read_text(encoding="utf-8"))
    assert selftest.personality_version(text) == len(history["text"]["personality.md"])


# ---------------------------------------------------------------- end to end: the owner-chat path, really

def vault(data_dir: Path, profile=True, goals=True) -> None:
    v = Path(data_dir) / "vault"
    if profile:
        (v / "10 Profile").mkdir(parents=True, exist_ok=True)
        (v / "10 Profile" / "me.md").write_text("Apprentice, Boxing, likes short answers.", encoding="utf-8")
    if goals:
        (v / "20 Goals").mkdir(parents=True, exist_ok=True)
        (v / "20 Goals" / "2026.md").write_text("Finish the apprenticeship with a 5.", encoding="utf-8")


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


def stop(proc):
    proc.terminate()
    proc.wait(timeout=10)


def tasks(migrated) -> list:
    from sqlmodel import Session, select

    from app.db import Task, make_engine
    with Session(make_engine(migrated.db_url)) as s:
        return s.exec(select(Task)).all()


def test_selftest_adds_a_real_task_through_the_owner_chat_path(tmp_path, migrated):
    vault(migrated.data_dir)
    proc, port = serve(tmp_path, migrated)
    try:
        r = post(port, "/api/selftest/tools?notify=0")
        assert r["ok"] is True, r
        c = r["checks"]
        assert c["hook_deny"] and c["hook_allow"] and c["mcp"] == "connected" and c["tools_missing"] == []
        assert c["task_added"] is True and c["profile"] and c["goals"] and c["personality"] == "current"
        assert c["voice"] is None and c["browser"] is None and c["memory"] is None   # not set up in dev: skipped, not failed
        proof = r["proof"]["task"]
        assert proof["title"].startswith("Kairo self-test") and proof["due"] and proof["removed"] is True
        assert tasks(migrated) == []                                       # the test task is gone again
        assert json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/health").read())["tools"] == "connected"
    finally:
        stop(proc)


def test_selftest_and_owner_chat_send_the_same_prompt_and_tools(tmp_path, migrated):
    """The self-test can't drift from what the owner's messages get (the false green of 2026-10-14)."""
    from websockets.sync.client import connect
    vault(migrated.data_dir)
    rec = tmp_path / "rec.json"
    proc, port = serve(tmp_path, migrated, f"KAIRO_HOOK_FAKE_RECORD={rec}\n")
    try:
        post(port, "/api/selftest/tools?notify=0&voice=0&browser=0")
        test_run = json.loads(rec.read_text())
        with connect(f"ws://127.0.0.1:{port}/ws/chat") as ws:
            ws.send(json.dumps({"type": "message", "text": "add task test tomorrow", "thread_id": None}))
            while json.loads(ws.recv(timeout=60))["type"] not in ("done", "error"):
                pass
        chat_run = json.loads(rec.read_text())
    finally:
        stop(proc)

    def flag(run, name):
        return run["argv"][run["argv"].index(name) + 1]

    for name in ("--mcp-config", "--allowedTools"):
        assert flag(test_run, name) == flag(chat_run, name), name
    strip = lambda t: "\n".join(l for l in t.splitlines() if not l.startswith("Right now:"))   # noqa: E731
    assert strip(test_run["system"]) == strip(chat_run["system"])
    assert "## 10 Profile/me.md" in chat_run["system"] and "## 20 Goals/2026.md" in chat_run["system"]


@pytest.mark.parametrize("extra,category,words", [
    ("KAIRO_HOOK_FAKE_NO_TOOLS=1\n", "toolchain", "no Kairo tools"),
    ("KAIRO_HOOK_FAKE_SKIP_TOOLS=1\n", "model", "didn't add"),
])
def test_selftest_fails_when_the_model_has_no_tools_or_does_not_act(tmp_path, migrated, extra, category, words):
    vault(migrated.data_dir)
    proc, port = serve(tmp_path, migrated, extra)
    try:
        r = post(port, "/api/selftest/tools?notify=0&voice=0&browser=0")
        assert r["ok"] is False and r["category"] == category and words in r["reason"], r
        today = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/today").read())
        assert any("Self-test failed" in i for i in today.get("issues", [])), today.get("issues")
    finally:
        stop(proc)


def test_empty_vault_is_a_yellow_to_do_not_a_failure(tmp_path, migrated):
    """A new install has no Profile/Goals yet: that's not broken (owner 2026-10-14)."""
    proc, port = serve(tmp_path, migrated)
    try:
        r = post(port, "/api/selftest/tools?notify=0&voice=0&browser=0")
        assert r["ok"] is True and r["checks"]["context"] == "empty", r
        assert any("onboarding" in w for w in r["warnings"])
        today = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/today").read())
        assert not any("Self-test" in i for i in today["issues"])                 # no red chip
        assert any("onboarding" in n for n in today["notices"])                  # a yellow one
    finally:
        stop(proc)


def test_selftest_catches_a_release_without_the_tool_bridge(tmp_path, migrated):
    """What every release so far looked like: no server/hooks, no server/mcp_kairo."""
    vault(migrated.data_dir)
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
        stop(proc)


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
