"""Backups + restore test (acceptance: restore to temp -> DB opens, vault complete)."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import backup
from app.db import Task, make_engine
from app.main import create_app


@pytest.fixture
def data(migrated):
    engine = make_engine(migrated.db_url)
    with Session(engine) as s:
        for i in range(5):
            s.add(Task(title=f"task {i}"))
        s.commit()
    engine.dispose()   # Windows locks open database files
    vault = migrated.data_dir / "vault"
    (vault / "20 Goals").mkdir(parents=True, exist_ok=True)
    (vault / "20 Goals" / "2026.md").write_text("Finish the apprenticeship. äöü", encoding="utf-8")
    (vault / "Kairo").mkdir(parents=True, exist_ok=True)
    (vault / "Kairo" / "note.md").write_text("x", encoding="utf-8")
    (migrated.data_dir / "secrets").mkdir(parents=True, exist_ok=True)
    (migrated.data_dir / "secrets" / "google-write.json").write_text("SECRET-TOKEN")
    return migrated


def make(settings, when=None):
    return backup.make(db_path=settings.db_path, data_dir=settings.data_dir, config_dir=settings.config_dir,
                       dest_dir=settings.data_dir / "backups" / "nightly", now=when)


def test_backup_contains_db_vault_config_but_no_secrets(data):
    z = make(data)
    with zipfile.ZipFile(z) as f:
        names = set(f.namelist())
        assert {"db/kairo.sqlite", "vault/20 Goals/2026.md", "vault/Kairo/note.md", "config/permissions.yaml", "manifest.json"} <= names
        assert not any("secret" in n.lower() or "google-write" in n for n in names)
        assert b"SECRET-TOKEN" not in b"".join(f.read(n) for n in names)
    result = backup.verify(z)
    assert result["rows"] >= 5 and result["vault_files"] == 2


def test_restore_test_catches_a_damaged_backup(data, tmp_path):
    z = make(data)
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(z) as src, zipfile.ZipFile(bad, "w") as dst:
        for n in src.namelist():
            body = src.read(n)
            if n == "vault/20 Goals/2026.md":
                body = b"tampered"
            dst.writestr(n, body)
    with pytest.raises(backup.BackupError, match="changed"):
        backup.verify(bad)
    missing = tmp_path / "missing.zip"
    with zipfile.ZipFile(z) as src, zipfile.ZipFile(missing, "w") as dst:
        for n in src.namelist():
            if n != "vault/Kairo/note.md":
                dst.writestr(n, src.read(n))
    with pytest.raises(backup.BackupError, match="missing"):
        backup.verify(missing)


def test_rotation_keeps_14_daily_and_8_weekly(tmp_path):
    d = tmp_path / "b"
    d.mkdir()
    start = dt.datetime(2026, 1, 1, 3, 30)
    for i in range(120):
        (d / f"kairo-{(start + dt.timedelta(days=i)):%Y%m%d-%H%M%S}.zip").write_bytes(b"x")
    backup.rotate(d)
    left = sorted(p.name for p in d.glob("*.zip"))
    weeks = {dt.datetime.strptime(n[6:21], "%Y%m%d-%H%M%S").isocalendar()[:2] for n in left}
    assert len(weeks) >= 8 and len(left) == 19   # 14 daily (3 weeks) + 5 older weeks
    assert left[-1] == f"kairo-{(start + dt.timedelta(days=119)):%Y%m%d-%H%M%S}.zip"


def test_real_restore_moves_the_current_data_aside(data):
    z = make(data)
    db = data.db_path
    con = sqlite3.connect(db)
    con.execute("DELETE FROM tasks")
    con.commit()
    con.close()
    (data.data_dir / "vault" / "20 Goals" / "2026.md").unlink()
    saved = backup.restore(z, data.data_dir)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 5
    con.close()
    assert (data.data_dir / "vault" / "20 Goals" / "2026.md").read_text(encoding="utf-8").endswith("äöü")
    assert (saved / "db" / "kairo.sqlite").is_file()   # nothing thrown away


KILLED_MID_WRITE = """
import os, sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("PRAGMA journal_mode=WAL")
con.execute("PRAGMA wal_autocheckpoint=0")
con.execute("DELETE FROM tasks")
con.commit()
os._exit(0)   # Stop-Process -Force: the last writes are still in kairo.sqlite-wal
"""


def test_restore_takes_the_wal_aside_with_the_database(data, tmp_path):
    """Kairo's database is in WAL mode (acceptance #1). A WAL left next to the restored file would be
    replayed into it; it belongs to the old database, so it moves aside with it."""
    z = make(data)
    db = data.db_path
    script = tmp_path / "killed.py"
    script.write_text(KILLED_MID_WRITE, encoding="utf-8")
    subprocess.run([sys.executable, str(script), str(db)], check=True, timeout=60)
    assert Path(f"{db}-wal").is_file()
    saved = backup.restore(z, data.data_dir)
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 5
    finally:
        con.close()
    old = sqlite3.connect(saved / "db" / "kairo.sqlite")          # the old database, its last writes included
    try:
        assert old.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    finally:
        old.close()


def test_backup_and_restore_jobs_and_api(data):
    app = create_app(data)
    pushes = []
    app.state.pusher.notify = lambda *a, **k: pushes.append(a)
    with TestClient(app) as client:
        run = app.state.jobs.run_job("backup")   # not forced: runs even in the quiet window
        assert run.status == "ok", run.error
        run = app.state.jobs.run_job("restore_test")
        assert run.status == "ok" and json.loads(run.output_path)["vault_files"] >= 2
        body = client.get("/api/backups").json()
        assert body["status"]["test_ok"] is True and len(body["backups"]) == 1
        assert pushes == []


def test_failed_restore_test_pushes_and_shows_in_the_brief(data):
    app = create_app(data)
    pushes = []
    app.state.pusher.notify = lambda *a, **k: pushes.append(a)
    with TestClient(app):
        assert app.state.jobs.run_job("restore_test").status == "error"   # no backup yet
        app.state.jobs.run_job("backup")
        z = backup.latest(data.data_dir / "backups" / "nightly")
        z.write_bytes(b"not a zip")
        assert app.state.jobs.run_job("restore_test").status == "error"
        assert pushes and pushes[-1][0] == "Restore test failed"
        from app.brief import _warnings
        with Session(make_engine(data.db_url)) as s:
            assert any("Restore test failed" in w for w in _warnings(s))
