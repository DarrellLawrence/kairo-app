"""F22: the owner acceptance tests run on the PC itself (docs/ACCEPTANCE.md, KICKOFF v8). Here: that
each automated test passes when Kairo behaves and FAILS when it doesn't, cleans up after itself,
never sends anything, never leaves the kill switch on, and that results reach the app and Run
diagnosis. On kairo-home the same tests run against the real Claude."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import acceptance, approvals, diagnosis, killswitch
from app.acceptance import MARK, Context, Turn
from app.db import Approval, ChatMessage, ChatThread, Task, make_engine
from app.gate import load_gate
from app.main import create_app
from app.settings import REPO_ROOT

TZ = ZoneInfo("Europe/Zurich")


def ctx_for(settings, *, turn=None, classify=None, selftest=None, broken=None) -> Context:
    async def no_turn(text):
        return Turn(reply="OK")

    async def green():
        return {"ok": True, "reason": ""}

    async def red():
        return {"tool server": {"ok": False, "reason": "not connected"}, "voice model": {"ok": False, "reason": "missing"}}
    return Context(engine=make_engine(settings.db_url), tz=TZ, turn=turn or no_turn,
                   classify_mail=classify or (lambda mail: [{"id": mail["id"], "suspicious": True}]),
                   gate=load_gate(REPO_ROOT / "config"), selftest=selftest or green, broken_selftests=broken or red)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 1 Tools

def acting_turn(settings, reply="Added: KAIRO-TEST buy milk, tomorrow 09:00.", days=1):
    async def turn(text):
        engine = make_engine(settings.db_url)
        due = (dt.datetime.now(TZ) + dt.timedelta(days=days)).replace(hour=9, minute=0, second=0, microsecond=0)
        with Session(engine) as s:
            s.add(Task(title=f"{MARK} buy milk", due=due))
            s.commit()
        return Turn(reply=reply, tools=["mcp__kairo__tasks_create"])
    return turn


def test_tools_passes_when_kairo_acts(migrated):
    ctx = ctx_for(migrated, turn=acting_turn(migrated))
    r = run(acceptance.t_tools(ctx))
    assert r["ok"], r
    with Session(ctx.engine) as s:
        assert s.exec(select(Task).where(Task.title.contains(MARK))).all() == []      # cleaned up


@pytest.mark.parametrize("turn_kw, failing", [
    ({"reply": "Sure. Which time tomorrow?"}, "no question back"),
    ({"reply": "Added.\nAnything else I can do?"}, "one-line confirmation"),
    ({"days": 2}, "due tomorrow"),
])
def test_tools_fails_when_kairo_doesnt(migrated, turn_kw, failing):
    r = run(acceptance.t_tools(ctx_for(migrated, turn=acting_turn(migrated, **turn_kw))))
    assert not r["ok"] and r["checks"][failing] is False


def test_tools_fails_without_a_task(migrated):
    r = run(acceptance.t_tools(ctx_for(migrated)))
    assert not r["ok"] and r["checks"]["the task exists"] is False


# ---------------------------------------------------------------- 7 Outbound safety

def card_turn(settings):
    async def turn(text):
        with Session(make_engine(settings.db_url)) as s:
            s.add(Approval(tool="gmail.send", args_json=json.dumps({"to": ["marc@example.com"], "subject": "hi", "body": "hi"}),
                           summary="Email marc@example.com: hi", tier=3, status="open"))
            s.commit()
        return Turn(reply="The mail waits for your approval.")
    return turn


def test_outbound_safety_passes_and_cleans_up(migrated):
    ctx = ctx_for(migrated, turn=card_turn(migrated))
    r = run(acceptance.t_outbound(ctx))
    assert r["ok"], r
    with Session(ctx.engine) as s:
        assert killswitch.is_paused(s) is False                                       # restored
        assert [c.status for c in s.exec(select(Approval)).all()] == ["rejected"]      # never left open, never sent


def test_outbound_safety_catches_a_broken_kill_switch(migrated, monkeypatch):
    monkeypatch.setattr(approvals, "check_approvable", lambda *a, **k: None)          # the P1.5 bug, back again
    r = run(acceptance.t_outbound(ctx_for(migrated, turn=card_turn(migrated))))
    assert not r["ok"] and r["checks"]["approve refused with the kill switch on"] is False
    with Session(make_engine(migrated.db_url)) as s:
        assert killswitch.is_paused(s) is False
        assert s.exec(select(Approval)).one().status == "failed"                      # the recording executor refused


def test_outbound_safety_fails_without_a_card_or_flag(migrated):
    r = run(acceptance.t_outbound(ctx_for(migrated, classify=lambda mail: [{"id": mail["id"], "suspicious": False}])))
    assert not r["ok"]
    assert r["checks"]["a card, nothing sent"] is False and r["checks"]["injection mail flagged"] is False


def test_outbound_keeps_a_paused_kairo_paused(migrated):
    ctx = ctx_for(migrated, turn=card_turn(migrated))
    with Session(ctx.engine) as s:
        killswitch.set_paused(s, True)
    run(acceptance.t_outbound(ctx))
    with Session(ctx.engine) as s:
        assert killswitch.is_paused(s) is True


# ---------------------------------------------------------------- 9 One thread, 10 Personality, 11 Self-test

def say(settings, *replies, threads=1):
    with Session(make_engine(settings.db_url)) as s:
        ids = []
        for _ in range(threads):
            t = ChatThread(brain="claude_cli")
            s.add(t)
            s.commit()
            ids.append(t.id)
        for i, text in enumerate(replies):
            s.add(ChatMessage(thread_id=ids[i % len(ids)], role="kairo", text=text))
        s.commit()


def test_one_thread(migrated):
    say(migrated, "Done.", "Added it.")
    assert run(acceptance.t_one_thread(ctx_for(migrated)))["ok"]
    say(migrated, "Sorry, I can't access your calendar in this session.")
    r = run(acceptance.t_one_thread(ctx_for(migrated)))
    assert not r["ok"] and "can't access" in r["detail"]


def test_two_threads_fail(migrated):
    say(migrated, "a", "b", threads=2)
    assert not run(acceptance.t_one_thread(ctx_for(migrated)))["ok"]


@pytest.mark.parametrize("reply, ok", [("Today: school 07:40. Test Friday: 2 study blocks set.\nRain at 17:00.", True),
                                       ("\n".join(f"line {i}" for i in range(9)), False),
                                       ("School at 07:40. Want me to plan the evening?", False)])
def test_personality(migrated, reply, ok):
    async def turn(text):
        return Turn(reply=reply)
    assert run(acceptance.t_personality(ctx_for(migrated, turn=turn)))["ok"] is ok


def test_selftest_honesty_needs_red_when_broken(migrated):
    async def still_green():
        return {"tool server": {"ok": True}, "voice model": {"ok": True}}
    assert run(acceptance.t_selftest(ctx_for(migrated)))["ok"]
    assert not run(acceptance.t_selftest(ctx_for(migrated, broken=still_green)))["ok"]


# ---------------------------------------------------------------- the runner, the app, Run diagnosis

def test_a_crashing_test_is_a_failed_test(migrated):
    async def boom(text):
        raise RuntimeError("CLI died")
    res = run(acceptance.run(ctx_for(migrated, turn=boom), version="v1", ids=[1, 10]))
    assert res[1]["ok"] is False and "CLI died" in res[1]["detail"]
    with Session(make_engine(migrated.db_url)) as s:
        last = acceptance.load(s)["last_run"]
        assert last["version"] == "v1" and last["total"] == 2 and last["passed"] == 0
        assert acceptance.due_after_release(s, "v1") is False and acceptance.due_after_release(s, "v2") is True


def test_app_report_manual_and_diagnosis(migrated):
    with TestClient(create_app(migrated)) as c:
        rep = c.get("/api/acceptance").json()
        assert [t["id"] for t in rep["tests"]] == list(range(1, 31))
        assert rep["tests"][11]["result"]["by"] == "ci" and "CI" in rep["tests"][11]["result"]["detail"]
        assert c.post("/api/acceptance/15/manual", json={"ok": True, "note": "silent switch on: heard"}).status_code == 200
        assert c.post("/api/acceptance/1/manual", json={"ok": True}).status_code == 400          # automated, not tappable
        assert c.post("/api/acceptance/15/manual", json={"ok": "yes"}).status_code == 400
        rep = c.get("/api/acceptance").json()
        assert rep["tests"][14]["result"]["ok"] is True and rep["summary"]["manual_passed"] == 1
    text = diagnosis.run(db_url=migrated.db_url, data_dir=migrated.data_dir, app_root=migrated.data_dir)["text"]
    assert "== Acceptance" in text and "PASS #15 (owner" in text


def test_acceptance_turns_stay_out_of_the_conversation_and_push_nothing(migrated):
    app = create_app(migrated)
    pushes = []
    app.state.pusher.notify = lambda *a, **k: pushes.append(a)
    with TestClient(app):
        with Session(make_engine(migrated.db_url)) as s:
            before = len(s.exec(select(ChatMessage)).all())
        turn = asyncio.run(app.state.acceptance_ctx().turn(f"add task {MARK} buy milk tomorrow"))
        assert turn.error is None and MARK in turn.reply                       # the fake CLI echoes the prompt
        with Session(make_engine(migrated.db_url)) as s:
            assert len(s.exec(select(ChatMessage)).all()) == before
        app.state.acceptance_running = True
        app.state.on_new_card(Approval(id=1, tool="gmail.send", args_json="{}", summary="x", tier=3))
        app.state.acceptance_running = False
        app.state.on_new_card(Approval(id=2, tool="gmail.send", args_json="{}", summary="x", tier=3))
    assert len(pushes) == 1


def test_broken_copies_turn_the_selftest_red(migrated):
    """Self-test honesty on throwaway copies: a tool server that can't start, a voice without models."""
    app = create_app(migrated)
    with TestClient(app):
        broken = asyncio.run(app.state.acceptance_ctx().broken_selftests())
    assert broken["tool server"]["ok"] is False and "tool server" in broken["tool server"]["reason"]
    assert broken["voice model"]["ok"] is False
    assert (migrated.workspace_dir / ".mcp.json").read_text().count("mcp_kairo_missing") == 0   # live config untouched


# ---------------------------------------------------------------- 29 Lag

def _cli_runner(tmp_path):
    import os

    from app.brain.claude_cli import ClaudeCLIBrain
    from app.chat import BrainRunner
    from tests.conftest import FAKE_CLAUDE
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    return BrainRunner(ClaudeCLIBrain(claude_bin=str(FAKE_CLAUDE), workspace_dir=tmp_path / "ws", timeout_s=20, env=env))


def test_lag_passes_streaming_warm_and_no_tool_server(migrated, tmp_path):
    ctx = ctx_for(migrated)
    ctx.runner = _cli_runner(tmp_path)
    r = run(acceptance.t_lag(ctx))
    assert r["ok"], r
    assert "cold: start" in r["detail"] and "warm: start" in r["detail"]
    assert ctx.runner.brain._warm is None                    # closed afterwards


def test_lag_fails_when_the_warm_process_is_not_used(migrated, tmp_path):
    ctx = ctx_for(migrated)
    ctx.runner = _cli_runner(tmp_path)
    ctx.runner.brain.warm_up = lambda **kw: False            # broken: nothing is started ahead
    r = run(acceptance.t_lag(ctx))
    assert not r["ok"] and r["checks"]["the call turn ran warm"] is False


# ---------------------------------------------------------------- 30 Health

def _health_ctx(migrated, monkeypatch, *, url_answers=True, free_gb=400):
    from collections import namedtuple

    from app import health
    U = namedtuple("U", "total used free")
    monkeypatch.setattr(health.shutil, "disk_usage", lambda p: U(500e9, 500e9 - free_gb * 1e9, free_gb * 1e9))
    logs = migrated.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "boot-check.json").write_text(json.dumps({"kairo_url": "https://kairo-home.tail1234.ts.net"}), encoding="utf-8")

    def get(url):
        if not url_answers:
            import httpx
            raise httpx.ConnectError("down")

    class Brain:
        def health(self):
            from app.brain import BrainStatus
            return BrainStatus.ok

    class Runner:
        brain = Brain()
    ctx = ctx_for(migrated)
    migrated.vault_dir.mkdir(parents=True, exist_ok=True)
    ctx.vault_dir, ctx.data_dir = migrated.vault_dir, migrated.data_dir
    from tests.fakes.fake_google import FakeGoogle, connect
    fake = FakeGoogle()
    connect(migrated.secrets_dir, fake)
    ctx.health = health.HealthWatch(settings=migrated, engine=ctx.engine, runner=Runner(), pusher=None, http_get=get,
                                    transport=fake.transport)
    return ctx


def test_health_passes_and_cleans_up(migrated, monkeypatch):
    ctx = _health_ctx(migrated, monkeypatch)
    r = run(acceptance.t_health(ctx))
    assert r["ok"], r
    assert not list((migrated.vault_dir / "Kairo").glob(f"{MARK}*"))
    assert not list((migrated.data_dir / "conflicts-resolved").rglob("*.md"))


def test_health_fails_when_the_tailnet_is_down_or_the_disk_full(migrated, monkeypatch):
    ctx = _health_ctx(migrated, monkeypatch, url_answers=False, free_gb=5)
    r = run(acceptance.t_health(ctx))
    assert not r["ok"]
    assert r["checks"]["the tailnet address answers"] is False and r["checks"]["at least 10 GB free"] is False
    assert "can't reach Kairo" in r["detail"]
