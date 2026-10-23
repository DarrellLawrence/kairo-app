import json
import os
import sys
from pathlib import Path

import pytest

from app.brain import BrainStatus, EventKind
from app.brain.claude_cli import ClaudeCLIBrain, build_env, classify_error
from tests.conftest import FAKE_CLAUDE


def make(tmp_path, flag=None, **env):
    base = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    base.update({k: str(v) for k, v in env.items()})
    return ClaudeCLIBrain(claude_bin=str(FAKE_CLAUDE), workspace_dir=tmp_path / "ws", timeout_s=10, env=base, flag=flag)


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


# ---------------------------------------------------------------- the conversation process (voice V1, 2026-10-15)
# One warm process for chat and calls: the model is switched per turn (control request set_model,
# verified with the real CLI 2.1.300) and a turn can be interrupted (control request interrupt) without
# losing the process. Written before the code.

def test_the_warm_process_switches_model_per_turn(tmp_path):
    rec = tmp_path / "rec.json"
    brain = make(tmp_path, KAIRO_HOOK_FAKE_RECORD=rec)
    assert brain.warm_up(key="conv", session_id=None, system="s", tools=[], model="haiku") is True
    try:
        a = list(brain.run("quick one", session_id=None, system="s", tools=[], warm="conv", model="haiku"))
        sid = a[-1].session_id
        b = list(brain.run("write my reply", session_id=sid, system="s", tools=[], warm="conv", model="sonnet"))
        c = list(brain.run("thanks", session_id=sid, system="s", tools=[], warm="conv", model="haiku"))
        data = json.loads(rec.read_text())
        assert data["models"] == ["haiku", "sonnet", "haiku"]          # one process, the model per turn
        assert [e[-1].data["usage"]["warm"] for e in (a, b, c)] == [True, True, True]
        assert b[-1].data["usage"]["model"] == "claude-sonnet-fake"
    finally:
        brain.close_warm()


def test_interrupt_stops_the_turn_and_keeps_the_process(tmp_path):
    rec = tmp_path / "rec.json"
    brain = make(tmp_path, KAIRO_HOOK_FAKE_RECORD=rec, KAIRO_HOOK_FAKE_MODE="long")
    brain.warm_up(key="conv", session_id=None, system="s", tools=[], model="haiku")
    try:
        events = []
        for ev in brain.run("tell me a long story", session_id=None, system="s", tools=[], warm="conv", model="haiku"):
            events.append(ev)
            if ev.kind is EventKind.text and len([e for e in events if e.kind is EventKind.text]) == 3:
                assert brain.interrupt() is True
        last = events[-1]
        assert last.kind is EventKind.error and last.data.get("interrupted") and last.data.get("cancelled")
        assert len([e for e in events if e.kind is EventKind.text]) < 40          # stopped early
        assert brain.health() is BrainStatus.ok                                   # an interrupt is not a failure
        pid = json.loads(rec.read_text())["pid"]
        after = list(brain.run("next", session_id=last.session_id, system="s", tools=[], warm="conv", model="haiku"))
        assert after[-1].kind is EventKind.result and json.loads(rec.read_text())["pid"] == pid
    finally:
        brain.close_warm()


def test_interrupt_when_nothing_runs_is_harmless(tmp_path):
    brain = make(tmp_path)
    assert brain.interrupt() is False


def test_a_conversation_process_lives_long(tmp_path):
    brain = make(tmp_path)
    brain.warm_up(key="conv", session_id=None, system="s", tools=[], model="haiku", idle_s=3600, max_age_s=12 * 3600)
    try:
        w = brain._warm
        w.started -= 2 * 3600                       # two hours old: still usable (the call process: 15 min)
        assert brain.warm_ready("conv", None)
    finally:
        brain.close_warm()


# ---------------------------------------------------------------- the end of the answer, and the tail after it (voice G1)
# Real stream (Claude Code 2.1.296, measured): the answer ends with stream_event message_delta
# (delta.stop_reason) + message_stop; then the CLI writes a summary of the turn (system/post_turn_summary,
# 1.9-3.4 s on Haiku) and only then sends the result. A tool turn has several message_stop events, only the
# last one has stop_reason end_turn.

def stream_line(event_type, **fields):
    return json.dumps({"type": "stream_event", "event": {"type": event_type, **fields}, "session_id": "s"})


def parsed(brain, *lines):
    return [e for line in lines for e in brain._parse_line(line)]


def test_message_end_is_sent_once_when_the_answer_ends(tmp_path):
    kinds = [e.kind for e in run(make(tmp_path), "hello")]
    assert kinds.count(EventKind.message_end) == 1
    assert kinds[-2:] == [EventKind.message_end, EventKind.result]       # after the last words, before the result
    assert kinds.index(EventKind.message_end) > max(i for i, k in enumerate(kinds) if k is EventKind.text)


def test_only_end_turn_ends_the_answer(tmp_path):
    brain = make(tmp_path)
    def stops(reason):
        return parsed(brain, stream_line("message_start"), stream_line("message_delta", delta={"stop_reason": reason}),
                      stream_line("message_stop"))
    assert [e.kind for e in stops("end_turn")] == [EventKind.message_end]
    for reason in ("tool_use", "max_tokens", "refusal", None):
        assert stops(reason) == [], reason
    # a message_stop without its message_delta is no end either, and a reason never carries over to the next message
    assert parsed(brain, stream_line("message_stop")) == []
    parsed(brain, stream_line("message_delta", delta={"stop_reason": "end_turn"}), stream_line("message_stop"))
    assert parsed(brain, stream_line("message_stop")) == []


def test_a_tool_turn_has_one_message_end_after_the_tool_call(tmp_path):
    marker = tmp_path / "marker"
    mcp = tmp_path / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"probe": {
        "command": sys.executable, "args": [str(FAKE_CLAUDE.parent / "probe_mcp.py")], "env": {"PROBE_MARKER": str(marker)}}}}))
    brain = make(tmp_path)
    brain.mcp_config = mcp
    events = run(brain, "touch it [[fake-tool touch {}]]", tools=["mcp__probe__touch"])
    kinds = [e.kind for e in events]
    assert marker.exists() and kinds.count(EventKind.tool_use) == 1
    assert kinds.count(EventKind.message_end) == 1                      # the tool call's message_stop (tool_use) is no end
    assert kinds.index(EventKind.tool_use) < kinds.index(EventKind.message_end) < kinds.index(EventKind.result)


def test_the_tail_after_the_answer_is_measured(tmp_path):
    events = run(make(tmp_path, KAIRO_HOOK_FAKE_TAIL_S="0.8"), "hello")
    usage = events[-1].data["usage"]
    assert usage["first_text_ms"] <= usage["last_text_ms"] <= usage["message_end_ms"] <= usage["result_ms"]
    assert usage["tail_ms"] == usage["result_ms"] - usage["message_end_ms"]
    assert 700 <= usage["tail_ms"] < 5000                               # the fake's 0.8 s, with room for a busy machine
    assert usage["message_end_ms"] <= usage["summary_ms"] <= usage["result_ms"]     # the summary event is what the tail was


def test_no_tail_without_a_summary(tmp_path):
    usage = run(make(tmp_path), "hello")[-1].data["usage"]
    assert 0 <= usage["tail_ms"] < 700 and "summary_ms" not in usage


def test_the_tail_is_measured_on_a_warm_process_too(tmp_path):
    brain = make(tmp_path, KAIRO_HOOK_FAKE_TAIL_S="0.8")
    brain.warm_up(key="conv", session_id=None, system="s", tools=[])
    try:
        usage = list(brain.run("hi", session_id=None, system="s", tools=[], warm="conv"))[-1].data["usage"]
        assert usage["warm"] is True and 700 <= usage["tail_ms"] < 5000
    finally:
        brain.close_warm()


def test_a_turn_that_never_ended_has_no_tail(tmp_path):
    usage = run(make(tmp_path, KAIRO_HOOK_FAKE_MODE="usage_limit"))[-1].data["usage"]
    assert "tail_ms" not in usage and "message_end_ms" not in usage


def test_the_interrupted_answer_ends_without_message_end(tmp_path):
    brain = make(tmp_path, KAIRO_HOOK_FAKE_MODE="long")
    brain.warm_up(key="conv", session_id=None, system="s", tools=[], model="haiku")
    try:
        events = []
        for ev in brain.run("tell me a long story", session_id=None, system="s", tools=[], warm="conv", model="haiku"):
            events.append(ev)
            if ev.kind is EventKind.text and len([e for e in events if e.kind is EventKind.text]) == 3:
                brain.interrupt()
        assert events[-1].kind is EventKind.error
        assert EventKind.message_end not in [e.kind for e in events]
    finally:
        brain.close_warm()


# ---------------------------------------------------------------- flag g1.cli_no_summary (voice G1; owner's decision 16.4)
# CLAUDE_CODE_CLASSIFIER_SUMMARY=0 stops the CLI's summary of each turn (the 2 s tail). The variable is
# undocumented, so it is only set when the owner switched the flag on; the flag is read whenever a process starts.

NO_SUMMARY = "CLAUDE_CODE_CLASSIFIER_SUMMARY"


def recorded_env(tmp_path, flag):
    rec = tmp_path / "rec.json"
    run(make(tmp_path, flag=flag, KAIRO_HOOK_FAKE_RECORD=rec))
    return json.loads(rec.read_text())["env"]


def test_the_flag_is_in_the_list_and_off_by_default():
    from app import flags
    default, what = flags.FLAGS["g1.cli_no_summary"]
    assert default is False and "undocumented" in what.lower()


def test_without_the_flag_the_cli_environment_is_unchanged(tmp_path):
    assert NO_SUMMARY not in recorded_env(tmp_path, flag=None)                    # no reader at all
    assert NO_SUMMARY not in recorded_env(tmp_path, flag=lambda name: False)      # flag off
    assert NO_SUMMARY not in build_env({"PATH": "/bin"})


def test_the_backends_own_setting_never_reaches_the_cli_unless_the_flag_sets_it(tmp_path):
    rec = tmp_path / "rec.json"
    run(make(tmp_path, flag=lambda name: False, KAIRO_HOOK_FAKE_RECORD=rec, **{NO_SUMMARY: "1"}))     # not on the allow-list
    assert NO_SUMMARY not in json.loads(rec.read_text())["env"]


def test_with_the_flag_on_the_cli_is_told_to_skip_its_summary(tmp_path):
    asked = []
    def flag(name):
        asked.append(name)
        return True
    assert recorded_env(tmp_path, flag=flag)[NO_SUMMARY] == "0"
    assert set(asked) == {"g1.cli_no_summary"}
    usage = run(make(tmp_path, flag=lambda name: True))[-1].data["usage"]
    assert usage["no_summary"] is True
    assert run(make(tmp_path, flag=lambda name: False))[-1].data["usage"]["no_summary"] is False


def test_a_flag_that_cant_be_read_counts_as_off(tmp_path):
    def broken(name):
        raise RuntimeError("no such table: settings_kv")
    assert NO_SUMMARY not in recorded_env(tmp_path, flag=broken)


def test_the_flag_changes_the_tail_the_cli_leaves_behind(tmp_path):
    slow = run(make(tmp_path, flag=lambda name: False, KAIRO_HOOK_FAKE_TAIL_S="0.8"))[-1].data["usage"]
    fast = run(make(tmp_path, flag=lambda name: True, KAIRO_HOOK_FAKE_TAIL_S="0.8"))[-1].data["usage"]
    assert slow["tail_ms"] >= 700 and "summary_ms" in slow
    assert fast["tail_ms"] < 500 and "summary_ms" not in fast


@pytest.fixture
def owner_flag(migrated):
    """The flag as the app reads it (app/flags.py in the database) and the owner's switch."""
    from sqlmodel import Session
    from app import flags
    from app.db import make_engine
    engine = make_engine(migrated.db_url)

    def read(name):
        with Session(engine) as s:
            return flags.on(s, name)

    def switch(value):
        with Session(engine) as s:
            flags.set_flag(s, "g1.cli_no_summary", value)
    return read, switch


def test_the_owner_flips_the_flag_and_the_next_process_follows(tmp_path, owner_flag):
    read, switch = owner_flag
    brain = make(tmp_path, flag=read, KAIRO_HOOK_FAKE_TAIL_S="0.8")
    assert run(brain)[-1].data["usage"]["tail_ms"] >= 700
    switch(True)
    assert run(brain)[-1].data["usage"]["tail_ms"] < 500
    switch(None)                                    # back to the default: off
    assert run(brain)[-1].data["usage"]["tail_ms"] >= 700


def test_a_warm_process_started_before_the_flip_is_replaced(tmp_path, owner_flag):
    read, switch = owner_flag
    brain = make(tmp_path, flag=read, KAIRO_HOOK_FAKE_TAIL_S="0.8")
    assert brain.warm_up(key="conv", session_id="abc", system="s", tools=[]) is True
    try:
        old = brain._warm
        assert brain.warm_ready("conv", "abc") and brain.warm_up(key="conv", session_id="abc", system="s", tools=[]) is False
        switch(True)
        assert not brain.warm_ready("conv", "abc")                                  # the old environment no longer fits
        assert brain.warm_up(key="conv", session_id="abc", system="s", tools=[]) is True      # same key: replaced, not kept
        assert brain._warm is not old and brain.warm_ready("conv", "abc")
        usage = list(brain.run("hi", session_id="abc", system="s", tools=[], warm="conv"))[-1].data["usage"]
        assert usage["warm"] is True and usage["no_summary"] is True and usage["tail_ms"] < 500
    finally:
        brain.close_warm()


def test_a_turn_never_runs_on_a_warm_process_with_the_old_setting(tmp_path, owner_flag):
    """No warm_up after the flip: the turn runs on a fresh process instead of waiting 2 s for a stale one."""
    read, switch = owner_flag
    brain = make(tmp_path, flag=read, KAIRO_HOOK_FAKE_TAIL_S="0.8")
    brain.warm_up(key="conv", session_id="abc", system="s", tools=[])
    try:
        switch(True)
        usage = list(brain.run("hi", session_id="abc", system="s", tools=[], warm="conv"))[-1].data["usage"]
        assert usage["warm"] is False and usage["no_summary"] is True and usage["tail_ms"] < 500
        assert brain._warm is None                  # the stale one is gone (a cold turn on its conversation retires it)
    finally:
        brain.close_warm()


def test_the_app_reads_the_owners_switch_for_the_conversation_process(migrated, tmp_path, monkeypatch):
    """GET/PUT /api/flags -> the next Claude process of the owner's conversation (both values)."""
    from fastapi.testclient import TestClient
    from app.main import create_app

    rec = tmp_path / "rec.json"
    monkeypatch.setenv("KAIRO_HOOK_FAKE_RECORD", str(rec))

    def converse(ws, text, thread_id=None):
        ws.send_json({"type": "message", "text": text, "thread_id": thread_id})
        while (frame := ws.receive_json())["type"] not in ("done", "error"):
            pass
        return frame

    with TestClient(create_app(migrated)) as client, client.websocket_connect("/ws/chat") as ws:
        flag = next(f for f in client.get("/api/flags").json() if f["name"] == "g1.cli_no_summary")
        assert flag["on"] is False and flag["default"] is False
        assert converse(ws, "one")["type"] == "done"
        before = json.loads(rec.read_text())
        assert NO_SUMMARY not in before["env"]                          # off: the CLI runs as before
        client.put("/api/flags/g1.cli_no_summary", json={"on": True})
        assert converse(ws, "two")["type"] == "done"
        after = json.loads(rec.read_text())
        assert after["env"][NO_SUMMARY] == "0" and after["pid"] != before["pid"]      # a new process, same conversation
        client.put("/api/flags/g1.cli_no_summary", json={"on": None})
        assert converse(ws, "three")["type"] == "done"
        assert NO_SUMMARY not in json.loads(rec.read_text())["env"]
        client.app.state.runner.brain.close_warm()


# ---------------------------------------------------------------- the alarm: a tail over 0.5 s (voice G1)
# With the switch off the tail is expected (about 2 s); with it on, a tail means the Claude Code version
# no longer honours the setting. Either way it is logged and shown on Voice speed (tail_notes).

def tail_logs(caplog):
    return [r for r in caplog.records if r.name == "kairo.brain" and r.getMessage().startswith("claude tail")]


def slow_notes(brain):
    return [(n["id"], n["title"], n["what"]) for n in brain.tail_notes()]


def test_a_tail_over_half_a_second_is_logged(tmp_path, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="kairo.brain"):
        run(make(tmp_path))                                                 # no tail
        assert not tail_logs(caplog)
        run(make(tmp_path, KAIRO_HOOK_FAKE_TAIL_S="0.8"))
    logged = tail_logs(caplog)
    assert len(logged) == 1 and logged[0].levelno == logging.INFO           # off: the tail is the known cost, no warning
    assert "g1.cli_no_summary" in logged[0].getMessage()                    # off: says what would remove it


def test_a_tail_with_the_switch_on_says_the_setting_stopped_working(tmp_path, caplog):
    import logging
    brain = make(tmp_path, flag=lambda name: True, KAIRO_HOOK_FAKE_TAIL_S="0.8", KAIRO_HOOK_FAKE_TAIL_ALWAYS="1")
    with caplog.at_level(logging.INFO, logger="kairo.brain"):
        usage = run(brain)[-1].data["usage"]
    assert usage["no_summary"] is True and usage["tail_ms"] >= 700
    text = " ".join(r.getMessage() for r in tail_logs(caplog))
    assert "CLAUDE_CODE_CLASSIFIER_SUMMARY=0" in text and "g1.cli_no_summary" not in text
    assert [r.levelno for r in tail_logs(caplog)] == [logging.WARNING]      # on and still slow: the real alarm


def test_the_log_line_comes_at_most_every_ten_minutes(tmp_path, caplog):
    import logging
    brain = make(tmp_path)

    def logged():
        return [r.getMessage() for r in tail_logs(caplog)]
    with caplog.at_level(logging.INFO, logger="kairo.brain"):
        for i in range(3):
            brain._note_tail(900, False, now=1000.0 + 5 * i)
        assert len(logged()) == 1
        brain._note_tail(900, False, now=1000.0 + 5 * 2 + 600)           # ten minutes after the first line
    assert len(logged()) == 2 and "3 slow answers" in logged()[1]        # what happened in between is counted
    brain._note_tail(400, False, now=5000.0)                             # a short tail is no alarm
    assert len(logged()) == 2


def test_the_expected_tail_never_logs_a_warning_but_the_unexpected_one_does(tmp_path, caplog):
    import logging
    brain = make(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="kairo.brain"):
        brain._note_tail(1900, False, now=1000.0)                            # switch off: the known cost
        brain._note_tail(1900, False, now=5000.0)
    assert len(tail_logs(caplog)) == 2
    assert [r for r in tail_logs(caplog) if r.levelno >= logging.WARNING] == []
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="kairo.brain"):
        brain._note_tail(1900, True, now=9000.0)                             # switch on and still slow: not expected
    assert [r.levelno for r in tail_logs(caplog)] == [logging.WARNING]


def test_slow_answers_with_the_switch_off_do_not_push_real_warnings_out_of_the_diagnosis(tmp_path):
    """A default install (switch off) has a slow tail on most answers. The diagnosis keeps the last 15
    warnings/errors; a real problem must still be in that list after an afternoon of chatting."""
    import logging
    import logging.handlers
    from app import diagnosis
    handler = logging.handlers.RotatingFileHandler(tmp_path / "kairo.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    before = (root.level, list(root.handlers))
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    try:
        logging.getLogger("kairo.push").warning("push failed for a reminder: 410 Gone")
        brain = make(tmp_path)
        for i in range(30):                                                  # one logged answer every ten minutes
            brain._note_tail(1900, False, now=1000.0 + 600 * i)
        handler.flush()
    finally:
        root.removeHandler(handler)
        root.setLevel(before[0])
        handler.close()
    report = diagnosis._Report()
    diagnosis._logs(report, tmp_path)
    kept = [line for line in report.lines if line.startswith("    ")]
    assert any("push failed" in line for line in kept)
    assert not any("claude tail" in line for line in kept)


def test_voice_speed_has_no_note_while_tails_are_short(tmp_path):
    brain = make(tmp_path)
    assert brain.tail_notes() == []
    run(brain)
    run(brain)
    assert brain.tail_notes() == []


def test_voice_speed_says_what_the_tail_costs_and_what_removes_it(tmp_path):
    brain = make(tmp_path)
    for ms in (2100, 1900, 300):
        brain._note_tail(ms, False)
    [(note_id, title, what)] = slow_notes(brain)
    assert note_id == "claude_tail" and "2.1 s" in title
    assert "2 of the last 3" in what and "Experiments" in what and "g1.cli_no_summary" in what


def test_voice_speed_warns_when_the_setting_does_not_work(tmp_path):
    brain = make(tmp_path)
    brain._note_tail(1900, True)
    [(note_id, title, what)] = slow_notes(brain)
    assert note_id == "claude_tail" and "still" in title.lower()
    assert "1 of the last 1" in what and "update" in what.lower()


def test_the_note_follows_the_switch(tmp_path):
    """Slow tails from before the owner switched it on don't count against the new setting."""
    brain = make(tmp_path)
    for _ in range(5):
        brain._note_tail(2000, False)
    assert brain.tail_notes()
    brain._note_tail(20, True)
    assert brain.tail_notes() == []
    brain._note_tail(1800, True)
    assert "still" in brain.tail_notes()[0]["title"].lower()


def test_the_note_keeps_only_the_last_answers(tmp_path):
    brain = make(tmp_path)
    brain._note_tail(2000, False)
    for _ in range(25):
        brain._note_tail(10, False)
    assert brain.tail_notes() == []


def test_voice_speed_shows_the_brains_note(migrated, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app

    def converse(ws, text):
        ws.send_json({"type": "message", "text": text, "thread_id": None})
        while (frame := ws.receive_json())["type"] not in ("done", "error"):
            pass

    monkeypatch.setenv("KAIRO_HOOK_FAKE_TAIL_S", "0.8")
    with TestClient(create_app(migrated)) as client, client.websocket_connect("/ws/chat") as ws:
        assert client.get("/api/voice/turns").json()["notes"] == []
        converse(ws, "one")
        [note] = client.get("/api/voice/turns").json()["notes"]
        assert note["id"] == "claude_tail" and "g1.cli_no_summary" in note["what"]
        client.put("/api/flags/g1.cli_no_summary", json={"on": True})
        converse(ws, "two")                                                  # a new process with the setting: no tail
        converse(ws, "three")
        assert client.get("/api/voice/turns").json()["notes"] == []
        client.app.state.runner.brain.close_warm()
