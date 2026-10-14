"""Secrets at rest (fix P1.10, diagnosis D5.7). Written before the code.
Google tokens used to be plain JSON that the owner and kairo-svc could both read; now they
are in the same box as the school-portal login (DPAPI for kairo-svc on Windows)."""

from __future__ import annotations

import json
import sys

from app import secretbox
from app.google.client import load_token, save_token, token_path


def test_google_tokens_are_not_stored_in_plain(tmp_path):
    save_token(tmp_path, "write", {"refresh_token": "rt-very-secret", "email": "owner@example.com"})
    raw = token_path(tmp_path, "write").read_bytes()
    assert b"rt-very-secret" not in raw
    assert load_token(tmp_path, "write") == {"refresh_token": "rt-very-secret", "email": "owner@example.com"}
    if sys.platform == "win32":
        assert secretbox.encrypted(token_path(tmp_path, "write"))


def test_old_plain_tokens_are_read_and_locked_away(tmp_path):
    token_path(tmp_path, "read").write_text(json.dumps({"refresh_token": "rt-old"}), encoding="utf-8")
    assert load_token(tmp_path, "read") == {"refresh_token": "rt-old"}
    assert b"rt-old" not in token_path(tmp_path, "read").read_bytes()
    assert load_token(tmp_path, "read") == {"refresh_token": "rt-old"}


def test_installer_keeps_no_plain_secrets():
    """Syncthing's admin password goes machine-encrypted to updater\\ (admins only); a BitLocker
    recovery key is never written into the setup log, and an old one is blanked out."""
    from app.settings import REPO_ROOT
    sync = (REPO_ROOT / "installer" / "KairoSync.psm1").read_text(encoding="ascii")
    assert "-Value \"user: kairo" not in sync and "Save-MachineSecret $P 'syncthing-gui.bin'" in sync
    setup = (REPO_ROOT / "installer" / "KairoSetup.psm1").read_text(encoding="ascii")
    body = setup[setup.index("function Enable-BitLockerSteps"):]
    body = body[:body.index("\nfunction ")]
    assert body.index("Stop-Transcript") < body.index("RECOVERY KEY") < body.index("Start-Transcript")
    install = (REPO_ROOT / "installer" / "Install-Kairo.ps1").read_text(encoding="ascii")
    assert install.index("Hide-RecoveryKeys") < install.index("Start-Transcript")
