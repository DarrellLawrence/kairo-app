"""Run diagnosis (More -> System): DIAGNOSIS section G from the phone. Owner only, read-only,
secrets redacted, every query valid against the real schema."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

import app.main as main_mod
from app import diagnosis
from app.db import (Approval, AuditLog, Capture, ChatMessage, ChatThread, JobRun, ModelRun, OpenLoop, SettingKV,
                    make_engine)
from app.main import create_app

# Built at run time, so no secret-shaped literal sits in the repo.
GOOGLE_ACCESS = "ya" + "29." + "a0AfB_byC" + "x" * 40
GOOGLE_REFRESH = "1/" + "/0e" + "Q" * 30
JWT = "ey" + "JhbGciOiJIUzI1NiJ9" + ".ey" + "JzdWIiOiIxMjM0In0" + ".c2lnbmF0dXJlLXZhbHVl"
RECOVERY = "-".join(["123456"] * 8)
OPAQUE = "Zk9" + "x7Qp" * 10
PASSWORD = "hunter2-" + "pw"
PUSH = "https://web.push.apple.com/" + "QG" + "r" * 60


def seed(settings) -> None:
    engine = make_engine(settings.db_url)
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        t = ChatThread(brain="claude_cli")
        s.add(t)
        s.commit()
        s.add_all([
            Approval(tool="gmail.send", args_json="{}", summary="x", tier=2, status="done", reason="Open loop: reply"),
            Approval(tool="gmail.send", args_json="{}", summary="x", tier=2, status="open"),
            OpenLoop(kind="reply", source="email", source_ref="m1", who="Alex", who_email="alex@example.com",
                     what="Answer Alex about Friday"),
            JobRun(job="morning_brief", started_at=now - dt.timedelta(minutes=5), finished_at=now, status="ok"),
            JobRun(job="email_triage", started_at=now - dt.timedelta(minutes=1), finished_at=now, status="error",
                   error=f"refresh failed: token={GOOGLE_REFRESH}"),
            ModelRun(purpose="chat", model="sonnet", ok=True, duration_ms=4200, input_tokens=10, output_tokens=5),
            ModelRun(purpose="call", model="haiku", ok=False, duration_ms=900),
            ModelRun(purpose="call", model="sonnet", ok=True, duration_ms=2100, startup_ms=40, first_text_ms=1300, warm=True),
            ModelRun(purpose="chat", model="sonnet", ok=True, duration_ms=3900, startup_ms=2400, first_text_ms=3600, warm=False),
            ChatMessage(thread_id=t.id, role="owner", text="add task test tomorrow", created_at=now - dt.timedelta(seconds=7)),
            ChatMessage(thread_id=t.id, role="kairo", text="Done.", created_at=now),
            ChatMessage(thread_id=t.id, role="owner", text="(spoken, on a call) hi", created_at=now - dt.timedelta(seconds=3)),
            ChatMessage(thread_id=t.id, role="kairo", text="Hi.", created_at=now),
            Capture(kind="audio", source="voice_note", status="filed", created_at=now - dt.timedelta(seconds=20), filed_at=now),
            AuditLog(actor="backend", action="proactive.ping", outcome="sent"),
            SettingKV(key="selftest.tools", value=json.dumps({"ok": True, "at": now.isoformat(), "seconds": 9,
                                                              "checks": {"mcp": "connected"}, "warnings": []})),
            SettingKV(key="capture.token", value=OPAQUE),
        ])
        s.commit()
    engine.dispose()


def logs(settings) -> None:
    d = settings.data_dir / "logs"
    d.mkdir(parents=True, exist_ok=True)
    (d / "kairo.log.1").write_text("2026-10-13 08:00:00,001 INFO app.push: quiet hours: no push for loop 3\n", encoding="utf-8")
    (d / "kairo.log").write_text("\n".join([
        "2026-10-14 09:00:00,123 WARNING app.push: push failed for " + PUSH + ": 410",
        f"2026-10-14 09:01:00,123 ERROR app.google: Authorization: Bearer {GOOGLE_ACCESS} rejected",
        f"2026-10-14 09:02:00,123 ERROR app.portal: login failed password={PASSWORD} for someone@example.com",
        f"2026-10-14 09:03:00,123 INFO app.x: recovery {RECOVERY} jwt {JWT} id {OPAQUE}",
        "2026-10-14 09:04:00,123 ERROR app.voice: Whisper failed to load",
        "Traceback (most recent call last):",
        "2026-10-14 09:05:00,123 WARNING app.serve: https://kairo-home.tail1234.ts.net slow, call +41 79 123 45 67",
    ]) + "\n", encoding="utf-8")
    import os
    for f in d.glob("kairo.log*"):         # Windows can stamp both files alike: order by rotation, not time
        os.utime(f, (1_700_000_000, 1_700_000_000))


def report(settings) -> str:
    return diagnosis.run(db_url=settings.db_url, data_dir=settings.data_dir, app_root=settings.data_dir / "no-app")["text"]


def test_every_query_runs_on_the_real_schema(migrated):
    seed(migrated)
    text = report(migrated)
    for section in ("G.1", "G.2", "G.3", "G.4", "G.5 ", "G.5a", "G.5b", "G.5c", "Last self-test", "G.6", "Logs"):
        assert f"== {section}" in text, section
    assert "ERROR" not in text and "no such" not in text, text
    assert "  done | 1 | 1" in text and "  open | 1 | 0" in text          # approvals, from loop Send
    assert "reply | email | open | 1" in text
    assert "chat 1 | 7.0 | 7.0" in text and "call 1 | 3.0 | 3.0" in text
    assert "call | warm | 1 | 40 | 40 | 1300 | 1300" in text and "chat | cold | 1 | 2400 | 2400 | 3600 | 3600" in text
    assert "briefs ok: 1" in text and "ok=True" in text


def test_it_never_writes(migrated):
    seed(migrated)
    db = diagnosis.db_path(migrated.db_url)
    before = hashlib.sha256(db.read_bytes()).hexdigest(), db.stat().st_mtime_ns
    report(migrated)
    assert (hashlib.sha256(db.read_bytes()).hexdigest(), db.stat().st_mtime_ns) == before
    c = diagnosis.connect_ro(db)
    with pytest.raises(sqlite3.OperationalError):
        c.execute("INSERT INTO settings(key, value) VALUES ('x', 'y')")
    c.close()


def test_secrets_are_redacted(migrated):
    seed(migrated)
    logs(migrated)
    text = report(migrated)
    for secret in (GOOGLE_ACCESS, GOOGLE_REFRESH, JWT, RECOVERY, OPAQUE, PASSWORD, PUSH, "someone@example.com",
                   "tail1234", "+41 79 123 45 67", "alex@example.com"):
        assert secret not in text, secret
    assert "s***@example.com" in text and "kairo-home.<tailnet>.ts.net" in text and "Authorization: Bearer <redacted>" in text
    # What the report is for survives.
    assert "'push failed' 1" in text and "'quiet hours: no push for' 1" in text and "'Whisper' 1" in text
    assert "'Traceback' 1" in text and "ERROR 3" in text and "token=<redacted google token>" in text and "2026-10-13 08:00:00 .. 2026-10-14 09:05:00" in text


@pytest.mark.parametrize("sample, kept", [
    ("2026-10-14 12:00:00 job email_triage 3 | 12.5 | 40.1", True),
    (r"C:\Kairo\app\releases\2026.10.14.1200-abc1234", True),
    ("purpose chat | sonnet | 12 | 0 | 4200 | 9000 | 1500 | 0.42", True),
])
def test_redaction_leaves_numbers_and_paths(sample, kept):
    assert (diagnosis.redact(sample) == sample) is kept


def test_no_database_is_reported_not_raised(settings):
    text = report(settings)
    assert "can't open read-only" in text and "== Logs" in text


def boot_check(settings, login: str | None) -> None:
    d = settings.data_dir / "logs"
    d.mkdir(parents=True, exist_ok=True)
    body = {"all_ok": True, "checks": []} | ({"tailnet_login": login} if login else {})
    (d / "boot-check.json").write_text(json.dumps(body), encoding="utf-8")


TAILNET = "https://kairo-home.tail1234.ts.net"


def test_owner_only(migrated, monkeypatch):
    monkeypatch.setattr(main_mod, "LOCAL_HOSTS", {"testclient", "127.0.0.1"})
    migrated = migrated.model_copy(update={"owner_check": True})
    me = {"tailscale-user-login": "Owner@Example.com"}
    with TestClient(create_app(migrated), base_url="http://127.0.0.1:8000") as c:
        r = c.post("/api/diagnosis")                                   # on the PC
        assert r.status_code == 200 and "== G.1" in r.json()["text"]
        assert c.post("/api/diagnosis", headers={"x-forwarded-for": "100.64.0.2"}).status_code == 403
        assert c.post("/api/diagnosis", headers={"origin": "https://evil.example"}).status_code == 403
        assert c.get("/api/diagnosis").status_code in (404, 405)       # only the POST, behind the origin check
    with TestClient(create_app(migrated), base_url="http://kairo.example.org") as c:
        assert c.post("/api/diagnosis", headers=me).status_code == 403  # not through Kairo's tailnet address
    with TestClient(create_app(migrated), base_url=TAILNET) as c:
        assert c.post("/api/diagnosis").status_code == 403             # no Tailscale person (tagged device)
        assert c.post("/api/diagnosis", headers=me).status_code == 200  # boot check hasn't pinned the account yet
        boot_check(migrated, "owner@example.com")
        assert c.post("/api/diagnosis", headers=me).status_code == 200
        assert c.post("/api/diagnosis", headers={"tailscale-user-login": "guest@example.com"}).status_code == 403
