"""Which Kairo version runs, and which one the installer installed (C:\\Kairo\\app\\state.json "current").

Owner's report 2026-10-19: an update said "ok" while the old server kept running. The two must match;
the health watch says so in red, with a push, when they don't."""

from __future__ import annotations

import json
from pathlib import Path

from .settings import REPO_ROOT


def running(app_root: Path = REPO_ROOT) -> str | None:
    """This process's release (release.json next to the code it started from), None in a dev checkout."""
    try:
        return str(json.loads((Path(app_root) / "release.json").read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def installed(app_root: Path = REPO_ROOT) -> str | None:
    """The release the installer switched to (releases\\<version> -> ..\\state.json), None outside an install."""
    root = Path(app_root)
    if root.parent.name.lower() != "releases":
        return None
    try:
        return str(json.loads((root.parent.parent / "state.json").read_text(encoding="utf-8-sig"))["current"]) or None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def older(a: str, b: str) -> bool:
    """Release a is older than b (versions are yyyy.MM.dd.HHmm-<sha>). Unknown forms: not older."""
    def key(v: str) -> tuple[int, ...] | None:
        try:
            return tuple(int(x) for x in v.split("-", 1)[0].split("."))
        except ValueError:
            return None
    ka, kb = key(a), key(b)
    return ka is not None and kb is not None and ka < kb
