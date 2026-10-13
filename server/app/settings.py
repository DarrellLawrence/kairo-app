"""Machine-specific settings, read from environment variables (.env).

Everything that differs between dev (laptop) and prod (kairo-home) lives here.
Nothing personal: who the owner is lives in config/profile.yaml.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseModel):
    data_dir: Path
    config_dir: Path
    db_url: str
    workspace_dir: Path
    brain: str
    claude_bin: str
    brain_timeout_s: int
    web_dist_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "kairo.sqlite"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def load_settings() -> Settings:
    data_dir = Path(_env("KAIRO_DATA_DIR", str(REPO_ROOT / "devdata")))
    return Settings(
        data_dir=data_dir,
        config_dir=Path(_env("KAIRO_CONFIG_DIR", str(REPO_ROOT / "config"))),
        db_url=_env("KAIRO_DB_URL", f"sqlite:///{data_dir / 'db' / 'kairo.sqlite'}"),
        workspace_dir=Path(_env("KAIRO_WORKSPACE_DIR", str(data_dir / "workspace"))),
        brain=_env("KAIRO_BRAIN", "claude_cli"),
        claude_bin=_env("KAIRO_CLAUDE_BIN", "claude"),
        brain_timeout_s=int(_env("KAIRO_BRAIN_TIMEOUT_S", "300")),
        web_dist_dir=Path(_env("KAIRO_WEB_DIST", str(REPO_ROOT / "web" / "dist"))),
    )


@lru_cache
def get_settings() -> Settings:
    return load_settings()
