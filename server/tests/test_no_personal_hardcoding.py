"""No personal data in the repo (owner's decision 2026-10-15; KICKOFF 15). config/profile.yaml holds
placeholders only; the real values live on the PC (data/config/profile.yaml, the vault, .env)."""

import re
import subprocess

import yaml

from app.settings import REPO_ROOT

FREEMAIL = ("gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "icloud.com", "me.com",
            "gmx.ch", "gmx.net", "gmx.de", "bluewin.ch", "yahoo.com", "proton.me", "protonmail.com")
FAKE = {"someone.else@gmail.com", "lea@gmail.com"}   # made up, used by the tests (a person on free mail)
EMAIL = re.compile(r"[a-z0-9._%+-]+@([a-z0-9-]+\.)+[a-z]{2,}", re.I)


PRIVATE = "private/"   # owner's decision 2026-10-19: their own profile, in the private repo on purpose; never scanned or printed


def tracked_files():
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=REPO_ROOT,
                         capture_output=True, text=True, check=True).stdout
    return [p for p in out.splitlines() if p and not p.startswith(PRIVATE)]


def test_repo_profile_is_placeholders_only():
    p = yaml.safe_load((REPO_ROOT / "config" / "profile.yaml").read_text(encoding="utf-8"))
    assert p["name"] == "Owner" and p["gmail"].endswith("@example.com")
    assert p["push_devices"] == [] and p["places"] == [] and p.get("school_portal") is None


def test_no_personal_email_address_anywhere():
    """Personal addresses are on free-mail domains; services and examples are fine."""
    offenders = []
    for rel in tracked_files():
        try:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for m in EMAIL.finditer(text):
            addr = m.group(0).lower()
            if addr.split("@", 1)[1] in FREEMAIL and addr not in FAKE:
                offenders.append(f"{rel}: {addr}")
    assert offenders == []
