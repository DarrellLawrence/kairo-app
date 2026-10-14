import json
import os
from pathlib import Path

import pytest

from app.brain import BrainStatus, EventKind
from app.brain.claude_cli import ClaudeCLIBrain, build_env, classify_error
from tests.conftest import FAKE_CLAUDE


def make(tmp_path, **env):
    base = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
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
    assert "--system-prompt" not in argv  # file, not argv
    assert Path(argv[argv.index("--system-prompt-file") + 1]).read_text(encoding="utf-8") == "You are Kairo."
    assert "--dangerously-skip-permissions" not in argv
    assert "bypassPermissions" not in argv


def test_runs_in_workspace_dir(tmp_path):
    rec = tmp_path / "rec.json"
    run(make(tmp_path, KAIRO_HOOK_FAKE_RECORD=rec))
    assert os.path.samefile(json.loads(rec.read_text())["cwd"], tmp_path / "ws")


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
    assert env == {"PATH": "/bin", "KAIRO_HOOK_DB": "y", "DISABLE_UPDATES": "1", "DISABLE_AUTOUPDATER": "1"}


def test_build_env_keeps_windows_essentials_case_insensitively():
    env = build_env({"SystemRoot": "C:\\Windows", "Path": "C:\\x", "GOOGLE_TOKEN": "s"})
    assert env["SystemRoot"] == "C:\\Windows" and env["Path"] == "C:\\x"
    assert "GOOGLE_TOKEN" not in env


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
    brain = ClaudeCLIBrain(claude_bin=str(tmp_path / "nope"), workspace_dir=tmp_path, env={"PATH": ""})
    assert brain.health() is BrainStatus.unavailable
    events = run(brain)
    assert events[-1].kind is EventKind.error and events[-1].status is BrainStatus.unavailable


def test_classify_error():
    assert classify_error("Please run /login", None) is BrainStatus.logged_out
    assert classify_error("x", 401) is BrainStatus.logged_out
    assert classify_error("Claude usage limit reached", None) is BrainStatus.usage_limited
    assert classify_error("x", 429) is BrainStatus.usage_limited
    assert classify_error("segfault", None) is BrainStatus.unavailable


def test_fake_probe_reports_identity(tmp_path):
    report = tmp_path / "probe.json"
    secret = tmp_path / "readable.txt"
    secret.write_text("x")
    run(make(tmp_path, KAIRO_HOOK_FAKE_PROBE=report, KAIRO_HOOK_FAKE_PROBE_PATHS=f"{secret};{tmp_path / 'missing.txt'}"))
    data = json.loads(report.read_text())
    assert data["user"]
    assert data["readable"] == {str(secret): True, str(tmp_path / "missing.txt"): False}


def test_images_go_in_as_one_stream_json_message(settings):
    from app.brain.claude_cli import ClaudeCLIBrain
    from tests.conftest import FAKE_CLAUDE
    brain = ClaudeCLIBrain(claude_bin=str(FAKE_CLAUDE), workspace_dir=settings.workspace_dir, mcp_config=None, timeout_s=20)
    events = list(brain.run("What is this letter?", session_id=None, system="s", tools=[], origin="job",
                            images=[(b"\x89PNG fake", "image/png")]))
    assert "--input-format" in brain.build_command(session_id=None, system="s", tools=[], images=True)
    assert "--input-format" not in brain.build_command(session_id=None, system="s", tools=[])
    final = [e for e in events if e.kind.value == "result"][-1].text
    assert "[1 image(s)] What is this letter?" in final


# -- fix plan 26, 27, 30: streaming, tool-less runs, warm process --------------------------------

def test_text_streams_as_deltas_and_is_not_sent_twice(tmp_path):
    rec = tmp_path / "rec.json"
    events = run(make(tmp_path, KAIRO_HOOK_FAKE_RECORD=rec), "hello there")
    assert "--include-partial-messages" in json.loads(rec.read_text())["argv"]
    texts = [e.text for e in events if e.kind is EventKind.text]
    assert len(texts) == 2                       # two deltas; the full assistant message is not repeated
    assert "".join(texts) == "echo: hello there"


def test_a_run_without_tools_starts_no_mcp_server(tmp_path):
    rec = tmp_path / "rec.json"
    cfg = tmp_path / "mcp.json"
    cfg.write_text('{"mcpServers": {}}')
    brain = make(tmp_path, KAIRO_HOOK_FAKE_RECORD=rec)
    brain.mcp_config = cfg
    run(brain, tools=[])
    argv = json.loads(rec.read_text())["argv"]
    assert "--mcp-config" not in argv
    assert "--strict-mcp-config" in argv          # and still no other MCP server either
    run(brain, tools=["mcp__kairo__tasks_list"])
    argv = json.loads(rec.read_text())["argv"]
    assert argv[argv.index("--mcp-config") + 1] == str(cfg)


def test_start_and_first_words_are_measured(tmp_path):
    usage = run(make(tmp_path))[-1].data["usage"]
    assert usage["warm"] is False
    assert isinstance(usage["startup_ms"], int) and isinstance(usage["first_text_ms"], int)
    assert usage["first_text_ms"] >= usage["startup_ms"] >= 0


def warm_brain(tmp_path, rec):
    brain = make(tmp_path, KAIRO_HOOK_FAKE_RECORD=rec)
    assert brain.warm_up(key="call", session_id=None, system="Warm system.", tools=[]) is True
    assert brain.warm_up(key="call", session_id=None, system="Warm system.", tools=[]) is False   # already there
    return brain


def test_a_warm_process_answers_several_turns(tmp_path):
    rec = tmp_path / "rec.json"
    brain = warm_brain(tmp_path, rec)
    try:
        first = list(brain.run("one", session_id=None, system="unused", tools=[], warm="call"))
        assert first[-1].kind is EventKind.result and first[-1].text == "echo: one"
        assert first[-1].data["usage"]["warm"] is True
        sid = first[-1].session_id
        assert brain.warm_ready("call", sid)       # the process now holds that conversation
        second = list(brain.run("two", session_id=sid, system="unused", tools=[], warm="call"))
        assert second[-1].text == "echo: two"
        data = json.loads(rec.read_text())
        assert data["turns"] == ["one", "two"]     # one process, two turns
        assert data["system"] == "Warm system."    # the system prompt it started with
        assert data["argv"][data["argv"].index("--input-format") + 1] == "stream-json"
    finally:
        brain.close_warm()


def test_a_different_key_or_conversation_runs_cold(tmp_path):
    rec = tmp_path / "rec.json"
    brain = warm_brain(tmp_path, rec)
    try:
        events = list(brain.run("chat", session_id=None, system="Cold system.", tools=[], warm="other"))
        assert events[-1].data["usage"]["warm"] is False
        assert json.loads(rec.read_text())["system"] == "Cold system."
        assert not brain.warm_ready("call", "some-other-session")
        assert brain.warm_ready("call", None)
    finally:
        brain.close_warm()


def test_a_cold_turn_on_the_warm_conversation_retires_the_warm_process(tmp_path):
    """Else the warm process would answer the next turn one turn behind."""
    brain = make(tmp_path)
    brain.warm_up(key="call", session_id="abc", system="s", tools=[])
    assert brain.warm_ready("call", "abc")
    run(brain, "typed in chat meanwhile", session_id="abc")
    assert not brain.warm_ready("call", "abc")


def test_a_dead_warm_process_falls_back_to_a_normal_run(tmp_path):
    brain = make(tmp_path)
    brain.warm_up(key="call", session_id=None, system="s", tools=[])
    brain._warm.proc.kill()
    brain._warm.proc.wait()
    events = list(brain.run("still answered", session_id=None, system="s", tools=[], warm="call"))
    assert events[-1].kind is EventKind.result and events[-1].text == "echo: still answered"
    assert events[-1].data["usage"]["warm"] is False
    assert brain._warm is None


def test_the_kill_switch_closes_the_warm_process(tmp_path):
    from app.chat import BrainRunner
    brain = make(tmp_path)
    brain.warm_up(key="call", session_id=None, system="s", tools=[])
    proc = brain._warm.proc
    BrainRunner(brain).cancel()
    assert brain._warm is None
    proc.wait(timeout=10)
