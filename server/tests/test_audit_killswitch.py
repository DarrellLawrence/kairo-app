"""Append-only audit log and the kill switch (KICKOFF 4, 16). Written first."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError
from sqlmodel import Session, select

from app.audit import args_hash, audit
from app.db import AuditLog, make_engine
from app.main import create_app


def test_audit_rows_cannot_be_changed_or_deleted(migrated):
    engine = make_engine(migrated.db_url)
    with Session(engine) as s:
        audit(s, actor="backend", action="gmail.label", outcome="executed", tier=2, args={"message_id": "a"})
    with engine.begin() as conn:
        with pytest.raises(DatabaseError):
            conn.execute(text("UPDATE audit_log SET outcome = 'nothing happened'"))
    with engine.begin() as conn:
        with pytest.raises(DatabaseError):
            conn.execute(text("DELETE FROM audit_log"))
    with Session(engine) as s:
        rows = s.exec(select(AuditLog)).all()
    assert len(rows) == 1 and rows[0].outcome == "executed"
    assert rows[0].args_hash == args_hash({"message_id": "a"})


def test_args_hash_is_order_independent():
    assert args_hash({"a": 1, "b": [2, 3]}) == args_hash({"b": [2, 3], "a": 1})
    assert args_hash({"a": 1}) != args_hash({"a": 2})


def test_kill_switch_api_and_audit(migrated):
    with TestClient(create_app(migrated)) as c:
        assert c.get("/api/kill-switch").json() == {"paused": False}
        assert c.post("/api/kill-switch", json={"paused": True}).json() == {"paused": True}
        assert c.get("/api/kill-switch").json() == {"paused": True}
        acts = [a["action"] for a in c.get("/api/activity").json()]
        assert "kill_switch.on" in acts
        c.post("/api/kill-switch", json={"paused": False})
        assert "kill_switch.off" in [a["action"] for a in c.get("/api/activity").json()]


def test_chat_refused_while_paused(migrated):
    with TestClient(create_app(migrated)) as c, c.websocket_connect("/ws/chat") as ws:
        c.post("/api/kill-switch", json={"paused": True})
        ws.send_json({"type": "message", "text": "hi"})
        frames = []
        while True:
            f = ws.receive_json()
            frames.append(f)
            if f["type"] in ("done", "error"):
                break
        assert frames[-1]["type"] == "error" and frames[-1]["status"] == "paused"


def test_kill_switch_stops_a_running_brain_call(migrated, monkeypatch):
    monkeypatch.setenv("KAIRO_HOOK_FAKE_MODE", "slow")   # fake CLI sleeps 30 s
    with TestClient(create_app(migrated)) as c, c.websocket_connect("/ws/chat") as ws:
        ws.send_json({"type": "message", "text": "long job"})
        assert ws.receive_json()["type"] == "thread"
        time.sleep(1.0)
        started = time.monotonic()
        c.post("/api/kill-switch", json={"paused": True})
        f = ws.receive_json()
        while f["type"] not in ("done", "error"):
            f = ws.receive_json()
        assert f["type"] == "error" and f["status"] == "paused"
        assert time.monotonic() - started < 10
