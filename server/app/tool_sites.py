"""Where the big optional packages live on kairo-home (voice, browser), and whether they really import.

The installer puts them in versioned folders (C:\\Kairo\\tools\\voice-<hash>) and writes the path
into the release (<release>\\voice-site.txt), so a rollback also switches back to the matching
packages. Without that file (dev, older installs) the old shared folder tools\\<kind> is used.

2026-10-14: pip --target left the package folders readable only by SYSTEM (owner-only temp dir,
moved into place), so for kairo-svc `faster_whisper` was an empty namespace package
("cannot import name 'WhisperModel' ... (unknown location)"). is_real_package() tells the two apart.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from .settings import REPO_ROOT


def site_file(kind: str) -> Path:
    return REPO_ROOT / f"{kind}-site.txt"


def configured(kind: str) -> bool:
    """The installer set this kind up for the running release (a failure then is a real failure)."""
    return site_file(kind).is_file()


def site(kind: str, data_dir: Path) -> Path:
    f = site_file(kind)
    if f.is_file():
        text = f.read_text(encoding="utf-8").strip()
        if text:
            return Path(text)
    return Path(data_dir).parent / "tools" / kind


def is_real_package(name: str) -> tuple[bool, str]:
    """(ok, detail). A folder Python can see but not read imports as an empty namespace package."""
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError) as exc:
        return False, f"{name}: {exc}"
    if spec is None:
        return False, f"{name}: not installed"
    if spec.origin in (None, "namespace") and spec.submodule_search_locations is not None:
        where = ", ".join(str(p) for p in spec.submodule_search_locations)
        return False, f"{name}: folder found but not readable by this account ({where})"
    return True, ""
