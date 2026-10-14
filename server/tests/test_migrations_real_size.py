"""Migrations on a real-sized database (fix P2.15, diagnosis D6.7). Downgrades were only ever
tested on an empty one. And why a failed update restores the backup instead of trusting a
downgrade: on SQLite a migration that fails half way keeps what it already did."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect

from tests.conftest import SERVER_DIR, alembic_config

ROWS = 20_000


def db_file(settings) -> Path:
    return Path(settings.db_url.removeprefix("sqlite:///"))


def fill(path: Path) -> dict[str, int]:
    db = sqlite3.connect(path)
    db.executemany("insert into tasks (title, priority, status, source, created_at) values (?, 0, 'open', 'chat', ?)",
                   [(f"task {i} " + "x" * 80, "2026-10-14 10:00:00") for i in range(ROWS)])
    db.executemany("insert into audit_log (ts, actor, action, outcome) values (?, 'brain', 'gmail.search', 'ok')",
                   [("2026-10-14 10:00:00",) for _ in range(ROWS)])
    db.commit()
    counts = {t: db.execute(f"select count(*) from {t}").fetchone()[0] for t in ("tasks", "audit_log")}
    db.close()
    return counts


def counts(path: Path) -> dict[str, int]:
    db = sqlite3.connect(path)
    try:
        return {t: db.execute(f"select count(*) from {t}").fetchone()[0] for t in ("tasks", "audit_log")}
    finally:
        db.close()


def revisions() -> list[str]:
    return sorted(p.name.split("_")[0] for p in (SERVER_DIR / "migrations" / "versions").glob("[0-9]*_*.py"))


def test_recent_migrations_go_down_and_up_without_losing_data(migrated):
    path = db_file(migrated)
    before = fill(path)
    assert path.stat().st_size > 2_000_000
    cfg = alembic_config(migrated.db_url)
    for target in revisions()[-4:-1]:
        command.downgrade(cfg, target)
        assert counts(path) == before, target
        command.upgrade(cfg, "head")
        assert counts(path) == before, target


def test_a_migration_that_fails_half_way_leaves_its_work_behind(migrated, tmp_path):
    """So the installer restores the backup it took before the update (Restore-Release)."""
    path = db_file(migrated)
    before = fill(path)
    backup = tmp_path / "before-update.sqlite"
    src, dst = sqlite3.connect(path), sqlite3.connect(backup)
    src.backup(dst)
    src.close()
    dst.close()
    scripts = tmp_path / "migrations"
    shutil.copytree(SERVER_DIR / "migrations", scripts, ignore=shutil.ignore_patterns("__pycache__"))
    (scripts / "versions" / "0999_broken.py").write_text(
        "from alembic import op\nimport sqlalchemy as sa\n"
        f"revision = '0999'\ndown_revision = '{revisions()[-1]}'\nbranch_labels = None\ndepends_on = None\n"
        "def upgrade():\n"
        "    op.create_table('ci_half', sa.Column('id', sa.Integer, primary_key=True))\n"
        "    op.execute('update tasks set title = title')\n"
        "    raise RuntimeError('half way')\n"
        "def downgrade():\n    op.drop_table('ci_half')\n", encoding="utf-8")
    cfg = alembic_config(migrated.db_url)
    cfg.set_main_option("script_location", str(scripts))
    with pytest.raises(RuntimeError):
        command.upgrade(cfg, "head")
    assert "ci_half" in inspect(create_engine(migrated.db_url)).get_table_names()   # half applied
    shutil.copyfile(backup, path)                                                 # what Restore-Release does
    assert "ci_half" not in inspect(create_engine(migrated.db_url)).get_table_names()
    assert counts(path) == before
