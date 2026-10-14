from __future__ import annotations

import os

os.environ.setdefault("KAIRO_SCHEDULER", "0")   # no background jobs in tests; test_reviews drives them directly
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from app.settings import REPO_ROOT, Settings

SERVER_DIR = Path(__file__).resolve().parents[1]
FAKE_CLAUDE = SERVER_DIR / "tests" / "fakes" / "fake_claude.py"


def alembic_config(db_url: str) -> Config:
    cfg = Config(str(SERVER_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    return Settings(
        data_dir=data,
        config_dir=REPO_ROOT / "config",
        db_url=f"sqlite:///{(data / 'db' / 'kairo.sqlite').as_posix()}",
        workspace_dir=data / "workspace",
        brain="claude_cli",
        claude_bin=str(FAKE_CLAUDE),
        brain_timeout_s=20,
        web_dist_dir=tmp_path / "no-web",
    )


@pytest.fixture
def migrated(settings: Settings) -> Settings:
    command.upgrade(alembic_config(settings.db_url), "head")
    return settings
