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
    config_dir: Path                 # what Kairo and the safety hook read
    db_url: str
    workspace_dir: Path
    brain: str
    claude_bin: str
    brain_timeout_s: int
    web_dist_dir: Path
    # Config in layers (fix P0.4): the owner's changes only. Set -> config_dir is built at start
    # from the release's config/ plus these (app/config_layers.py). Unset (dev, tests): config_dir as is.
    config_overrides: Path | None = None
    # Owner only, app-wide (main.OwnerOnly). Only the test fixtures turn it off; no env variable can.
    owner_check: bool = True

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "kairo.sqlite"

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / "secrets"

    @property
    def vault_dir(self) -> Path:
        return self.data_dir / "vault"

    @property
    def port(self) -> int:
        return int(os.environ.get("KAIRO_PORT") or 8000)


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def load_settings() -> Settings:
    data_dir = Path(_env("KAIRO_DATA_DIR", str(REPO_ROOT / "devdata")))
    overrides = _env("KAIRO_CONFIG_OVERRIDES", "")
    return Settings(
        data_dir=data_dir,
        # KAIRO_CONFIG_DIR stays for releases from before the layers (a rollback reads it).
        config_dir=data_dir / "config-effective" if overrides else Path(_env("KAIRO_CONFIG_DIR", str(REPO_ROOT / "config"))),
        config_overrides=Path(overrides) if overrides else None,
        db_url=_env("KAIRO_DB_URL", f"sqlite:///{(data_dir / 'db' / 'kairo.sqlite').as_posix()}"),
        workspace_dir=Path(_env("KAIRO_WORKSPACE_DIR", str(data_dir / "workspace"))),
        brain=_env("KAIRO_BRAIN", "claude_cli"),
        claude_bin=_env("KAIRO_CLAUDE_BIN", "claude"),
        brain_timeout_s=int(_env("KAIRO_BRAIN_TIMEOUT_S", "300")),
        web_dist_dir=Path(_env("KAIRO_WEB_DIST", str(REPO_ROOT / "web" / "dist"))),
    )


@lru_cache
def get_settings() -> Settings:
    return load_settings()
