"""Fix F24: what the installer, the updater and the boot check write (as an admin or as SYSTEM) lives
in C:\\Kairo\\status: only admins can change it, Kairo's own account can only read it. data\\ is Kairo's
account's to change, so an elevated write there could be steered somewhere else through a link.

Older installs wrote these files into data\\ (logs\\boot-check.json, updates\\last-update.json,
updates\\selftest.json): they are read there until the new place has one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

LEGACY = {"boot-check.json": "logs", "last-update.json": "updates", "selftest.json": "updates"}


def status_dir(data_dir: Path) -> Path:
    env = os.environ.get("KAIRO_STATUS_DIR", "").strip()
    return Path(env) if env else Path(data_dir).parent / "status"


def path(data_dir: Path, name: str) -> Path:
    new = status_dir(data_dir) / name
    if new.is_file() or name not in LEGACY:
        return new
    old = Path(data_dir) / LEGACY[name] / name
    return old if old.is_file() else new


def read_json(data_dir: Path, name: str):
    """The file's JSON, None when there is none; {"error": ...} when it can't be read."""
    p = path(data_dir, name)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {"error": f"{name} unreadable"}
