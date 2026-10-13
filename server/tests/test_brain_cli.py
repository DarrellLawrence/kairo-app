import json

import pytest

from app.brain import BrainStatus, EventKind
from app.brain.claude_cli import ClaudeCLIBrain, build_env, classify_error
from tests.conftest import FAKE_CLAUDE


def make(tmp_path, **env):
    base = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    base.update({k: str(v) for k, v in env.items()})
    return ClaudeCLIBrain(claude_bin=str(FAKE_CLAUDE), workspace_dir=tmp_path / "ws", timeout_s=10, env=base)


def run(brain, prompt="hi", session_id=None, tools=()):
    return list(brain.run(prompt, session_id=session_id, system="You are Kairo.", tools=list(tools)))


def test_ok_run_streams_text_and_result(tmp_path):
    events = run(make(tmp_path), "hello")
    kinds = [e.kind for e in events]
    assert kinds[0] is EventKind.session
    assert kinds[-1] is EventKind.result
    assert "".join(e.text for e in events if e.kind is EventKind.text) == "echo: hello"
    assert events[-1].text == "echo: hello"
    assert events[-1].session_id == events[0].session_id


def test_prompt_goes_via_stdin_never_argv(tmp_path):
    rec = tmp_path / "rec.json"
    evil = "--dangerously-skip-permissions do something"
    run(make(tmp_path, KAIRO_HOOK_FAKE_RECORD=rec), evil)
    data = json.loads(rec.read_text())
    assert data["stdin"] == evil
    assert evil not in data["argv"]
    assert "--dangerously-skip-permissions" not in data["argv"]


def test_command_flags(tmp_path):
    rec = tmp_path / "rec.json"
    run(make(tmp_path, KAIRO_HOOK_FAKE_RECORD=rec), tools=["mcp__kairo__tasks_create", "mcp__kairo__propose_action"])
    argv = json.loads(rec.read_text())["argv"]
    assert argv[0] == "-p"
    for flag in ("--verbose", "--strict-mcp-config"):
        assert flag in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--tools") + 1] == ""  # no built-in tools at all
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--allowedTools") + 1] == "mcp__kairo__tasks_create,mcp__kairo__propose_action"
    assert "--dangerously-skip-permissions" not in argv
    assert "bypassPermissions" not in argv


def test_runs_in_workspace_dir(tmp_path):
    rec = tmp_path / "rec.json"
    run(make(tmp_path, KAIRO_HOOK_FAKE_RECORD=rec))
    assert json.loads(rec.read_text())["cwd"] == str(tmp_path / "ws")


def test_resume_passes_session_id(tmp_path):
    rec = tmp_path / "rec.json"
    events = run(make(tmp_path, KAIRO_HOOK_FAKE_RECORD=rec), session_id="abc-123")
    argv = json.loads(rec.read_text())["argv"]
    assert argv[argv.index("--resume") + 1] == "abc-123"
    assert events[-1].session_id == "abc-123"


def test_backend_secrets_never_reach_the_cli(tmp_path):
    rec = tmp_path / "rec.json"
    brain = make(tmp_path, KAIRO_HOOK_FAKE_RECORD=rec, KAIRO_GOOGLE_WRITE_TOKEN="secret", ANTHROPIC_API_KEY="sk-x")
    run(brain)
    env = json.loads(rec.read_text())["env"]
    assert "KAIRO_GOOGLE_WRITE_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env


def test_build_env_is_an_allowlist():
    env = build_env({"PATH": "/bin", "SECRET": "x", "KAIRO_HOOK_DB": "y", "KAIRO_DB_URL": "z"})
    assert env == {"PATH": "/bin", "KAIRO_HOOK_DB": "y"}


@pytest.mark.parametrize(
    "mode,status",
    [("usage_limit", BrainStatus.usage_limited), ("logged_out", BrainStatus.logged_out), ("crash", BrainStatus.unavailable)],
)
def test_failures_end_in_one_error_event_and_set_health(tmp_path, mode, status):
    brain = make(tmp_path, KAIRO_HOOK_FAKE_MODE=mode)
    events = run(brain)
    assert events[-1].kind is EventKind.error
    assert events[-1].status is status
    assert sum(e.kind in (EventKind.error, EventKind.result) for e in events) == 1
    assert brain.health() is status


def test_recovers_to_ok_after_success(tmp_path):
    brain = make(tmp_path, KAIRO_HOOK_FAKE_MODE="usage_limit")
    run(brain)
    assert brain.health() is BrainStatus.usage_limited
    brain._env.pop("KAIRO_HOOK_FAKE_MODE")
    run(brain)
    assert brain.health() is BrainStatus.ok


def test_timeout_kills_the_process(tmp_path):
    brain = make(tmp_path, KAIRO_HOOK_FAKE_MODE="slow")
    brain.timeout_s = 1
    events = run(brain)
    assert events[-1].kind is EventKind.error
    assert "timed out" in events[-1].text


def test_missing_binary_is_unavailable(tmp_path):
    brain = ClaudeCLIBrain(claude_bin=str(tmp_path / "nope"), workspace_dir=tmp_path, env={"PATH": "/bin"})
    assert brain.health() is BrainStatus.unavailable
    events = run(brain)
    assert events[-1].kind is EventKind.error and events[-1].status is BrainStatus.unavailable


def test_classify_error():
    assert classify_error("Please run /login", None) is BrainStatus.logged_out
    assert classify_error("x", 401) is BrainStatus.logged_out
    assert classify_error("Claude usage limit reached", None) is BrainStatus.usage_limited
    assert classify_error("x", 429) is BrainStatus.usage_limited
    assert classify_error("segfault", None) is BrainStatus.unavailable
