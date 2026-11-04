"""The backend endpoint every model tool call goes through (via mcp_kairo)."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import app.main as main_mod
from app.db import Approval, AuditLog, Task, make_engine
from app.executor import Executor
from app.gate import Decision
from app.main import create_app
from app.tools import ToolRunner
from tests.conftest import FAKE_CLAUDE, SERVER_DIR, alembic_config
from tests.fakes.fake_google import FakeGoogle, connect, decode_raw


@pytest.fixture
def setup(migrated, monkeypatch):
    monkeypatch.setattr(main_mod, "LOCAL_HOSTS", {"testclient", "127.0.0.1"})
    fake = FakeGoogle()
    fake.add_message("m1", sender="Mr. Keller <keller@school.example>", subject="Projekt", body="Bitte bis Freitag.")
    connect(migrated.data_dir / "secrets", fake)
    app = create_app(migrated)
    app.state.google_transport = fake.transport()
    app.state.executor = Executor(secrets_dir=migrated.data_dir / "secrets", transport=fake.transport())
    client = TestClient(app)
    token = app.state.tool_token
    app.state.runner.current_origin = "owner_chat"   # as if a chat run were in progress

    def call(name, arguments, headers=None):
        h = {"authorization": f"Bearer {token}", **(headers or {})}
        return client.post("/internal/tools/call", json={"name": name, "arguments": arguments}, headers=h)

    yield dict(app=app, client=client, call=call, fake=fake, engine=make_engine(migrated.db_url), token=token)
    client.close()


def audit_rows(engine):
    with Session(engine) as s:
        return s.exec(select(AuditLog)).all()


def test_requires_the_per_start_token(setup):
    c = setup["client"]
    assert c.post("/internal/tools/call", json={"name": "tasks_list", "arguments": {}}).status_code == 403
    assert c.post("/internal/tools/call", json={"name": "tasks_list", "arguments": {}},
                  headers={"authorization": "Bearer wrong"}).status_code == 403
    assert c.get("/internal/tools/list").status_code == 403


def test_not_reachable_through_tailscale_serve(setup):
    r = setup["call"]("tasks_list", {}, headers={"x-forwarded-for": "100.64.0.2"})
    assert r.status_code == 403


def test_tool_list_has_no_write_tools(setup):
    tools = setup["client"].get("/internal/tools/list", headers={"authorization": f"Bearer {setup['token']}"}).json()["tools"]
    names = {t["name"] for t in tools}
    assert names == {"gmail_search", "gmail_read", "calendar_list", "vault_read", "tasks_list", "tasks_create",
                     "tasks_update", "notes_write", "propose_action",
                     # phase 3: Kairo's own tables only (no Google, no money)
                     "shopping_list", "shopping_add", "people_list", "people_note", "finance_list", "health_list",
                     # v7: Kairo's own tag table (calendar types)
                     "calendar_tag",
                     # open loops: Kairo's own table
                     "loops_list", "loops_add", "loops_snooze", "loops_close",
                     "debrief", "settings_list", "settings_set", "place_set", "place_remove",
                     # v8 B: quick answers and wishlists, Kairo's own tables
                     "captures_answer", "wishlist_add",
                     # v8 C6: search the owner's notes (read only)
                     "memory_search",
                     # v8 E: coaching log (Kairo's own table) and status (read)
                     "coach_log", "coach_status",
                     # v8 G: the owner's own profile into the vault
                     "profile_import", "profile_apply",
                     # knowledge calls: Kairo's own questions and the owner's profile/people notes
                     "knowledge_ask_later", "knowledge_answered", "profile_learn",
                     # document gauntlet: Kairo-owned vault file + mail to the owner's own address
                     "document_create",
                     # 2026-11-04: Kairo tidies its own pending questions (status only, undoable)
                     "pending_list", "pending_clear", "pending_restore"}
    propose = [t for t in tools if t["name"] == "propose_action"][0]
    assert "gmail.send" in propose["inputSchema"]["properties"]["action"]["enum"]
    assert not any("pay" in a or "money" in a for a in propose["inputSchema"]["properties"]["action"]["enum"])


def test_internal_tool_runs_and_is_audited(setup):
    r = setup["call"]("tasks_create", {"title": "Thermo revision"}).json()
    assert r["is_error"] is False
    with Session(setup["engine"]) as s:
        assert s.exec(select(Task)).one().title == "Thermo revision"
    assert any(a.action == "tasks.create" and a.outcome == "ok" for a in audit_rows(setup["engine"]))


def test_tier2_runs_in_the_backend(setup):
    r = setup["call"]("propose_action", {"action": "gmail.label", "args": {"message_id": "m1", "add": ["Kairo/Reply"]}}).json()
    assert r["is_error"] is False and r["text"].startswith("Done")
    assert setup["fake"].labels["Kairo/Reply"] in setup["fake"].messages["m1"]["labelIds"]


def test_outbound_becomes_a_card_and_sends_nothing(setup):
    cards = []
    setup["app"].state.on_new_card = cards.append
    r = setup["call"]("propose_action", {"action": "gmail.send", "args": {"to": ["x@example.com"], "subject": "Hi", "body": "Hello"},
                                         "reason": "owner asked"}).json()
    assert "Approval card #" in r["text"] and r["is_error"] is False
    assert setup["fake"].sent == []
    assert len(cards) == 1 and cards[0].reason == "owner asked"
    listed = setup["client"].get("/api/approvals").json()
    assert listed[0]["action"] == "gmail.send" and listed[0]["status"] == "open"


def test_approve_through_the_api_sends_exactly_once(setup):
    setup["call"]("propose_action", {"action": "gmail.send", "args": {"to": ["x@example.com"], "subject": "Hi", "body": "Hello"}})
    card_id = setup["client"].get("/api/approvals").json()[0]["id"]
    r = setup["client"].post(f"/api/approvals/{card_id}/approve", json={})
    assert r.status_code == 200 and r.json()["status"] == "done"
    assert decode_raw(setup["fake"].sent[0]["raw"]).get_content().strip() == "Hello"
    assert setup["client"].post(f"/api/approvals/{card_id}/approve", json={}).status_code == 409
    assert len(setup["fake"].sent) == 1


def test_approve_from_another_site_is_refused(setup):
    setup["call"]("propose_action", {"action": "gmail.send", "args": {"to": ["x@example.com"], "subject": "Hi", "body": "Hello"}})
    card_id = setup["client"].get("/api/approvals").json()[0]["id"]
    r = setup["client"].post(f"/api/approvals/{card_id}/approve", json={}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert setup["fake"].sent == []


@pytest.mark.parametrize("name,args", [
    ("Bash", {"command": "rm -rf /"}),
    ("gmail_send", {"to": ["x@example.com"]}),
    ("propose_action", {"action": "payments.send", "args": {"amount": 100}}),
    ("propose_action", {"action": "calendar.create_hold", "args": {"summary": "x", "start": "2026-10-20T10:00:00+02:00",
                                                                   "end": "2026-10-20T11:00:00+02:00", "attendees": [{"email": "x@example.com"}]}}),
])
def test_denied_calls_do_nothing_and_are_audited(setup, name, args):
    r = setup["call"](name, args).json()
    assert r["is_error"] is True and r["text"].startswith("Not allowed")
    assert setup["fake"].sent == [] and setup["fake"].events == {}
    assert any(a.outcome.startswith("denied") for a in audit_rows(setup["engine"]))


def test_no_run_in_progress_means_no_tools(setup):
    setup["app"].state.runner.current_origin = None
    assert setup["call"]("tasks_list", {}).json()["is_error"] is True


def test_jobs_cannot_create_cards(setup):
    setup["app"].state.runner.current_origin = "job"
    r = setup["call"]("propose_action", {"action": "gmail.reply", "args": {"message_id": "m1", "body": "ok"}}).json()
    assert r["is_error"] is True
    with Session(setup["engine"]) as s:
        assert s.exec(select(Approval)).all() == []


def test_workspace_has_fail_closed_hook_and_only_kairo_mcp(migrated):
    create_app(migrated)
    ws = migrated.workspace_dir
    settings = json.loads((ws / ".claude" / "settings.json").read_text())
    hook = settings["hooks"]["PreToolUse"][0]
    assert hook["matcher"] == "*"
    h = hook["hooks"][0]
    assert h["onFailure"] == "block" and h["timeout"] <= 60 and h["args"][0].endswith("pre_tool_use.py")
    assert "Bash" in settings["permissions"]["deny"] and "WebFetch" in settings["permissions"]["deny"]
    mcp = json.loads((ws / ".mcp.json").read_text())
    assert list(mcp["mcpServers"]) == ["kairo"]
    assert "google" not in json.dumps(mcp).lower()


# ---------------------------------------------------------------- mcp_kairo over a real socket

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_mcp_kairo_speaks_mcp_and_forwards(tmp_path, migrated):
    port = free_port()
    env_file = tmp_path / "kairo.env"
    env_file.write_text(f"KAIRO_DATA_DIR={migrated.data_dir}\nKAIRO_DB_URL={migrated.db_url}\nKAIRO_CLAUDE_BIN={FAKE_CLAUDE}\nKAIRO_PORT={port}\n")
    server = subprocess.Popen([sys.executable, "-m", "app.serve", "--env-file", str(env_file)], cwd=SERVER_DIR,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except OSError:
                time.sleep(0.25)
        mcp = json.loads((migrated.workspace_dir / ".mcp.json").read_text())["mcpServers"]["kairo"]
        env = {**os.environ, **mcp["env"]}
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "tasks_list", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "nope"},
        ]
        out = subprocess.run([mcp["command"], *mcp["args"]], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                             env=env, capture_output=True, text=True, timeout=30)
        replies = {r["id"]: r for r in map(json.loads, out.stdout.splitlines())}
        assert replies[1]["result"]["protocolVersion"] == "2025-06-18"
        assert "propose_action" in {t["name"] for t in replies[2]["result"]["tools"]}
        # no brain run in progress -> the backend refuses, and the bridge reports it as a tool error
        assert replies[3]["result"]["isError"] is True
        assert replies[4]["error"]["code"] == -32601
        assert set(replies) == {1, 2, 3, 4}
    finally:
        server.terminate()
        server.wait(timeout=10)


# ---------------------------------------------------------------- v8 block B: quick questions + wishlists

def test_wishlist_add_through_the_tool(setup):
    r = setup["call"]("wishlist_add", {"person": "Anna", "title": "Board game", "price_chf": 39.9,
                                       "url": "https://shop.example/game"})
    body = json.loads(r.json()["text"]) if "text" in r.json() else r.json()
    out = json.dumps(body)
    assert "Board game" in out and "Anna" in out
    r = setup["call"]("people_list", {"name": "Anna"})
    assert "Board game" in json.dumps(r.json())


def test_a_quick_answer_files_the_capture_again(setup):
    from app import capture
    from app.db import Capture
    with Session(setup["engine"]) as s:
        cap = Capture(kind="text", text="https://www.vinted.ch/items/77", source="share", status="asking",
                      question="Was the jacket from Anna? Gift idea?")
        s.add(cap)
        s.commit()
        s.refresh(cap)
        cap_id = cap.id
    filed = []
    setup["app"].state.jobs.run_job = lambda *a, **k: filed.append(a)   # filing itself is tested in test_jobs
    r = setup["call"]("captures_answer", {"capture_id": cap_id, "answer": "Yes, from Anna. Gift idea."})
    assert r.status_code == 200 and "filing it now" in json.dumps(r.json())
    with Session(setup["engine"]) as s:
        c = s.get(Capture, cap_id)
        assert c.status == "new" and "Owner answered: Yes, from Anna" in c.text
    time.sleep(0.2)
    assert filed and filed[0][0] == "capture_process"
    r = setup["call"]("captures_answer", {"capture_id": cap_id, "answer": "again"})
    assert "no question open" in json.dumps(r.json())
    with Session(setup["engine"]) as s:
        assert capture.quick_questions(s) == []


def test_quick_questions_reach_the_chat_prompt(setup):
    from app.db import Capture
    app = setup["app"]
    assert "Quick questions waiting" not in app.state.system_prompt()
    with Session(setup["engine"]) as s:
        s.add(Capture(kind="text", text="x", source="share", status="asking", question="Was the bag from Anna? Gift idea?"))
        s.commit()
    prompt = app.state.system_prompt()
    assert "Quick questions waiting" in prompt and "Was the bag from Anna? Gift idea?" in prompt and "captures_answer" in prompt


# ---------------------------------------------------------------- a busy database or a bug: a clear tool error, never a 500
# (acceptance #1: "add task ... tomorrow" got "the Kairo backend returned an error twice")

def test_a_due_date_without_a_zone_is_the_owners_local_day(setup, migrated):
    from app.profile import load_profile
    tz = load_profile(migrated.config_dir).tz
    r = setup["call"]("tasks_create", {"title": "Buy milk", "due": "2026-10-20"})        # "tomorrow": a date alone
    assert r.status_code == 200 and r.json()["is_error"] is False, r.text
    assert json.loads(r.json()["text"])["due"].startswith("2026-10-20T12:00:00")          # told back in local time
    r = setup["call"]("tasks_create", {"title": "Call the dentist", "due": "2026-10-20T09:00:00"})
    assert r.status_code == 200 and r.json()["is_error"] is False, r.text
    task_id = json.loads(r.json()["text"])["id"]
    r = setup["call"]("tasks_update", {"id": task_id, "due": "2026-10-21T08:30:00"})
    assert r.status_code == 200 and r.json()["is_error"] is False, r.text
    with Session(setup["engine"]) as s:
        due = {t.title: t.due.astimezone(tz) for t in s.exec(select(Task)).all()}
    assert (due["Buy milk"].date(), due["Buy milk"].hour) == (dt.date(2026, 10, 20), 12)   # noon, like a captured task
    assert (due["Call the dentist"].date(), due["Call the dentist"].hour, due["Call the dentist"].minute) == (dt.date(2026, 10, 21), 8, 30)


def test_the_backend_waits_for_a_busy_database_and_reads_never_block_writes(setup):
    with setup["app"].state.engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA busy_timeout").scalar() >= 30_000
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"


def test_a_task_is_added_while_another_writer_holds_the_lock(setup, migrated):
    held, done = threading.Event(), threading.Event()

    def job_writing():                                   # a job or the memory worker in the middle of a write
        con = sqlite3.connect(migrated.db_path, timeout=30)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT INTO settings (key, value) VALUES ('test.busy', '1')")
            held.set()
            time.sleep(1.5)
            con.commit()
        finally:
            con.close()
            done.set()

    threading.Thread(target=job_writing, daemon=True).start()
    assert held.wait(10)
    r = setup["call"]("tasks_create", {"title": "Buy milk", "due": "2026-10-20T09:00:00"})
    assert r.status_code == 200 and r.json()["is_error"] is False, r.text
    assert done.wait(10)
    with Session(setup["engine"]) as s:
        assert [t.title for t in s.exec(select(Task)).all()] == ["Buy milk"]


def test_a_long_read_does_not_stop_a_task_being_added(setup, migrated):
    reader = sqlite3.connect(migrated.db_path, timeout=30)   # a backup or a job reading for a while
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM tasks").fetchone()
        started = time.monotonic()
        r = setup["call"]("tasks_create", {"title": "Buy milk"})
        assert r.status_code == 200 and r.json()["is_error"] is False, r.text
        assert time.monotonic() - started < 5
    finally:
        reader.rollback()
        reader.close()


def test_an_unexpected_failure_is_a_readable_tool_error_and_logged(setup, monkeypatch, caplog):
    def broken(self, a):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(ToolRunner, "_tasks_create", broken)
    caplog.set_level(logging.ERROR, logger="kairo.tools")
    r = setup["call"]("tasks_create", {"title": "Buy milk"})
    assert r.status_code == 200
    assert r.json() == {"text": "Error: RuntimeError: disk on fire", "is_error": True}
    assert any(a.action == "tasks.create" and a.outcome == "error" for a in audit_rows(setup["engine"]))
    rec = next(x for x in caplog.records if x.name == "kairo.tools")
    assert "tasks.create" in rec.getMessage() and rec.exc_info and rec.exc_info[0] is RuntimeError


def test_a_database_error_reads_short_without_the_sql(setup, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def locked(self, a):
        raise OperationalError("INSERT INTO tasks (title) VALUES (?)", ("Buy milk",), sqlite3.OperationalError("database is locked"))
    monkeypatch.setattr(ToolRunner, "_tasks_create", locked)
    r = setup["call"]("tasks_create", {"title": "Buy milk"}).json()
    assert r == {"text": "Error: OperationalError: database is locked", "is_error": True}


def test_a_failing_tool_never_turns_a_denial_into_a_run(setup, migrated, monkeypatch):
    ran = []

    def records(self, a):
        ran.append(a)
        raise RuntimeError("x")
    monkeypatch.setattr(ToolRunner, "_tasks_create", records)
    setup["app"].state.runner.current_origin = None                          # no brain run: refused before any tool
    assert setup["call"]("tasks_create", {"title": "x"}).json()["text"].startswith("Not allowed")
    setup["app"].state.runner.current_origin = "owner_chat"
    r = setup["call"]("tasks_create", {"title": "x", "evil": True}).json()   # fails the schema: the gate denies
    assert r["is_error"] is True and r["text"].startswith("Not allowed")
    assert ran == []
    runner = ToolRunner(engine=setup["engine"], vault_dir=migrated.vault_dir, google_read=lambda: None)
    for verdict in ("deny", "approve", "execute"):
        out = runner.run(Decision(verdict, "no", "tasks.create", 1, {"title": "x"}))
        assert out == "Error: tasks.create is not a direct tool"
    assert ran == []
