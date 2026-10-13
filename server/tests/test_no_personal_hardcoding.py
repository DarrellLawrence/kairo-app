"""Acceptance check (KICKOFF 16): personal values only in config/profile.yaml, docs, .env."""

import subprocess

from app.profile import load_profile
from app.settings import REPO_ROOT

ALLOWED_PREFIXES = ("config/profile.yaml", "docs/", "CLAUDE.md", ".env")


def tracked_files():
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout
    return [p for p in out.splitlines() if p and not p.startswith(ALLOWED_PREFIXES)]


def test_owner_values_not_hardcoded():
    profile = load_profile(REPO_ROOT / "config")
    needles = [profile.name.lower(), *(d.lower() for d in profile.push_devices)]
    if profile.gmail:
        needles.append(profile.gmail.lower())
    offenders = []
    for rel in tracked_files():
        path = REPO_ROOT / rel
        try:
            text = path.read_text(encoding="utf-8").lower()
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        offenders += [f"{rel}: {n}" for n in needles if n in text]
    assert offenders == []
