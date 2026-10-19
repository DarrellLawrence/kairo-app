"""v7: cheapest model per job, usage per model."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import models
from app.brain.claude_cli import ClaudeCLIBrain, _usage
from app.chat import AskResult
from app.db import ModelRun, make_engine
from app.main import create_app


def test_model_table_and_overrides():
    assert models.pick("triage") == "haiku" and models.pick("lerndoku") == "sonnet" and models.pick("review_big") == "opus"
    assert models.pick("triage", {"models": {"triage": "sonnet"}}) == "sonnet"
    assert models.pick("triage", {"models": {"triage": "gpt-9"}}) == "haiku"   # only known aliases
    assert models.pick("something new") == "sonnet"


def test_cli_gets_the_model_flag(tmp_path):
    b = ClaudeCLIBrain(claude_bin="claude", workspace_dir=tmp_path, mcp_config=None, timeout_s=5)
    cmd = b.build_command(session_id=None, system="s", tools=[], model="haiku")
    assert cmd[cmd.index("--model") + 1] == "haiku"
    assert "--model" not in b.build_command(session_id=None, system="s", tools=[])


def test_usage_parsed_from_the_cli_result_line():
    msg = {"duration_ms": 2793, "total_cost_usd": 0.00057, "modelUsage": {"claude-haiku-5-5": {
        "inputTokens": 2, "outputTokens": 4, "cacheReadInputTokens": 2162, "cacheCreationInputTokens": 2859}}}
    assert _usage(msg) == {"model": "claude-haiku-5-5", "duration_ms": 2793, "cost_usd": 0.00057,
                           "input_tokens": 5023, "output_tokens": 4}


def test_jobs_ask_with_their_model_and_usage_is_shown(migrated):
    app = create_app(migrated)
    seen = []

    def ask(prompt, system, purpose="summary", model=None, images=None):
        seen.append((purpose, model))
        app.state.runner.on_usage(purpose, {"model": f"claude-{model}-5-5", "duration_ms": 3000, "cost_usd": 0.001,
                                            "input_tokens": 100, "output_tokens": 20}, True)
        return AskResult('{"headline": "x", "top3": []}')

    app.state.jobs._ask = ask
    with TestClient(app) as c:
        assert app.state.jobs.run_job("morning_brief", forced=True).status == "ok"
        assert seen == [("brief", "haiku")]
        u = c.get("/api/usage?days=1").json()
        assert u["by_model"]["haiku"]["runs"] == 1 and u["by_purpose"]["brief"]["tokens"] == 120
        assert u["models"]["review_big"] == "opus"


def test_why_an_owner_turn_was_not_on_haiku_is_stored_and_shown(migrated):
    """Owner's rule 2026-10-19: More -> Usage lists the owner's own turns that didn't run on Haiku, with why."""
    app = create_app(migrated)
    note = app.state.runner.on_usage
    note("call", {"model": "claude-haiku-5-5"}, True)
    note("call", {"model": "claude-sonnet-5-5"}, True, reason='"should I"')
    note("brief", {"model": "claude-sonnet-5-5"}, True)                  # a job, not the owner's turn
    note("call", None, False, reason="settings")                          # failed before a model ran
    note("chat", {"model": "claude-opus-5-5"}, True, reason="settings")
    note("money", {"model": "claude-opus-5-5"}, True, reason="money")
    with Session(make_engine(migrated.db_url)) as s:
        assert [r.reason for r in s.exec(select(ModelRun).order_by(ModelRun.id)).all()] == [
            None, '"should I"', None, "settings", "settings", "money"]
    with TestClient(app) as c:
        u = c.get("/api/usage?days=1").json()
    assert [(e["model"], e["purpose"], e["reason"]) for e in u["escalations"]] == [
        ("opus", "money", "money"), ("opus", "chat", "settings"), ("sonnet", "call", '"should I"')]
    assert all(e["ts"] for e in u["escalations"])


def test_only_the_last_twenty_escalations_are_shown(migrated):
    app = create_app(migrated)
    for i in range(25):
        app.state.runner.on_usage("call", {"model": "claude-sonnet-5-5"}, True, reason=f'"should I" {i}')
    with TestClient(app) as c:
        esc = c.get("/api/usage?days=1").json()["escalations"]
    assert len(esc) == models.ESCALATIONS == 20 and esc[0]["reason"] == '"should I" 24' and esc[-1]["reason"] == '"should I" 5'
