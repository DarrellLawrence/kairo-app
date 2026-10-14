"""Auto-update handshake: restart only when idle, not in the quiet hours unless
urgent; one push after each update."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app import housekeeping
from app.main import create_app

PC = "http://127.0.0.1:8000"


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setattr(main_mod, "LOCAL_HOSTS", {"testclient", "127.0.0.1"})


def quiet(monkeypatch, value):
    monkeypatch.setattr(main_mod, "in_quiet_hours", lambda *a, **k: value)


def test_ready_when_idle_then_chats_wait(migrated, local, monkeypatch):
    quiet(monkeypatch, False)
    with TestClient(create_app(migrated), base_url=PC) as c:
        assert c.post("/api/update/ready", json={}).json() == {"ready": True, "reason": "idle"}
        with c.websocket_connect("/ws/chat") as ws:
            ws.send_json({"type": "message", "text": "hi"})
            msg = ws.receive_json()
        assert msg["type"] == "error" and msg["status"] == "updating"
        assert c.post("/api/update/release").json() == {"ok": True}
        with c.websocket_connect("/ws/chat") as ws:
            ws.send_json({"type": "message", "text": "hi"})
            assert ws.receive_json()["type"] == "thread"


def test_not_ready_while_a_chat_or_job_runs(migrated, local, monkeypatch):
    quiet(monkeypatch, False)
    app = create_app(migrated)
    monkeypatch.setattr(type(app.state.runner), "busy", property(lambda self: True))
    with TestClient(app, base_url=PC) as c:
        assert c.post("/api/update/ready", json={"urgent": True}).json() == {"ready": False, "reason": "busy"}


def test_quiet_hours_wait_unless_urgent(migrated, local, monkeypatch):
    quiet(monkeypatch, True)
    with TestClient(create_app(migrated), base_url=PC) as c:
        assert c.post("/api/update/ready", json={}).json() == {"ready": False, "reason": "quiet"}
        assert c.post("/api/update/ready", json={"urgent": True}).json()["ready"] is True


def test_only_from_the_pc(migrated, local):
    with TestClient(create_app(migrated), base_url="https://kairo-home.tail1234.ts.net") as c:
        assert c.post("/api/update/ready", json={}).status_code == 403
        assert c.post("/api/update/release").status_code == 403


def test_one_push_after_an_update(migrated, monkeypatch):
    app = create_app(migrated)
    sent = []
    monkeypatch.setattr(app.state.pusher, "notify", lambda title, body, url="/", **k: sent.append(body))
    path = migrated.data_dir / "updates" / "last-update.json"
    path.parent.mkdir(parents=True)
    # Windows PowerShell writes UTF-8 with a BOM.
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"message": "Kairo updated to 2026.10.14.0900-abc1234.",
                                                  "notified": False}).encode())
    hk = housekeeping.Housekeeping(app)
    monkeypatch.setattr(housekeeping, "in_quiet_hours", lambda *a, **k: True)
    hk.update_notice()
    assert sent == []                      # night: waits for the morning
    monkeypatch.setattr(housekeeping, "in_quiet_hours", lambda *a, **k: False)
    hk.update_notice()
    hk.update_notice()
    assert sent == ["Kairo updated to 2026.10.14.0900-abc1234."]


# ---------------------------------------------------------------- fix P2.14: nothing running when Kairo restarts

def test_not_ready_while_a_job_a_call_or_the_browser_runs(migrated, local, monkeypatch):
    from sqlmodel import Session

    from app.db import Call, make_engine
    from app.portal import browser as portal_browser
    quiet(monkeypatch, False)
    app = create_app(migrated)
    with TestClient(app, base_url=PC) as c:
        app.state.jobs._queue.acquire()                       # a job is running
        try:
            assert c.post("/api/update/ready", json={"urgent": True}).json() == {"ready": False, "reason": "job running"}
        finally:
            app.state.jobs._queue.release()
        with Session(make_engine(migrated.db_url)) as s:
            call = Call(reason="r", opening="o", status="answered")
            s.add(call)
            s.commit()
            assert c.post("/api/update/ready", json={"urgent": True}).json() == {"ready": False, "reason": "call live"}
            call.status = "ended"
            s.add(call)
            s.commit()
        monkeypatch.setattr(portal_browser, "ACTIVE", 1)
        assert c.post("/api/update/ready", json={"urgent": True}).json() == {"ready": False, "reason": "browser open"}
        monkeypatch.setattr(portal_browser, "ACTIVE", 0)
        assert c.post("/api/update/ready", json={"urgent": True}).json()["ready"] is True


def test_jobs_wait_while_an_update_is_installing(migrated, local, monkeypatch):
    quiet(monkeypatch, False)
    app = create_app(migrated)
    ran = []
    app.state.jobs.bodies["test.job"] = lambda ctx: ran.append(1)
    with TestClient(app, base_url=PC) as c:
        assert c.post("/api/update/ready", json={}).json()["ready"] is True
        run = app.state.jobs.run_job("test.job", forced=True)
        assert run.status == "skipped" and "update" in run.error and ran == []
        c.post("/api/update/release")
        assert app.state.jobs.run_job("test.job", forced=True).status == "ok" and ran == [1]


def test_runs_cut_off_by_a_restart_are_marked_interrupted(migrated):
    from sqlmodel import Session, select

    from app.db import JobRun, make_engine
    with Session(make_engine(migrated.db_url)) as s:
        s.add(JobRun(job="email_triage", status="running"))
        s.commit()
    with TestClient(create_app(migrated)):
        pass
    with Session(make_engine(migrated.db_url)) as s:
        run = s.exec(select(JobRun).where(JobRun.job == "email_triage")).one()
        assert run.status == "interrupted" and run.finished_at is not None and "restart" in run.error
