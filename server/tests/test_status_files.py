"""Fix F24: what the installer, updater and boot check write (elevated) lives in C:\\Kairo\\status, which
Kairo's own account can only read; data\\ (Kairo's to change) only as the old place, read until the new
one has the file."""

from __future__ import annotations

import json

from app import housekeeping, status_files
from app.main import create_app, pinned_owner_login
from app.voice import COMPLETE, models_dir


def _write(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps(data).encode())   # Windows PowerShell writes a BOM


def test_status_folder_first_then_the_old_place(tmp_path, monkeypatch):
    monkeypatch.delenv("KAIRO_STATUS_DIR", raising=False)
    data = tmp_path / "data"
    assert status_files.status_dir(data) == tmp_path / "status"
    assert status_files.read_json(data, "boot-check.json") is None
    _write(data / "logs" / "boot-check.json", {"tailnet_login": "old@example.com"})
    assert status_files.read_json(data, "boot-check.json")["tailnet_login"] == "old@example.com"
    _write(tmp_path / "status" / "boot-check.json", {"tailnet_login": "new@example.com"})
    assert status_files.read_json(data, "boot-check.json")["tailnet_login"] == "new@example.com"
    assert pinned_owner_login(data) == "new@example.com"
    monkeypatch.setenv("KAIRO_STATUS_DIR", str(tmp_path / "elsewhere"))
    assert status_files.status_dir(data) == tmp_path / "elsewhere"


def test_unreadable_is_said_not_raised(tmp_path):
    p = tmp_path / "status" / "selftest.json"
    p.parent.mkdir(parents=True)
    p.write_text("{nope", encoding="utf-8")
    assert status_files.read_json(tmp_path / "data", "selftest.json") == {"error": "selftest.json unreadable"}


def test_the_updaters_note_is_pushed_once_without_writing_to_it(migrated, monkeypatch):
    note = migrated.data_dir.parent / "status" / "last-update.json"
    _write(note, {"message": "Kairo updated to 2026.10.15.", "ok": True, "at": "2026-10-15T03:00:00", "notified": False})
    before = note.read_bytes()
    app = create_app(migrated)
    sent = []
    monkeypatch.setattr(app.state.pusher, "notify", lambda title, body, url="/", **k: sent.append(body))
    monkeypatch.setattr(housekeeping, "in_quiet_hours", lambda *a, **k: False)
    hk = housekeeping.Housekeeping(app)
    hk.update_notice()
    hk.update_notice()
    assert sent == ["Kairo updated to 2026.10.15."]
    assert note.read_bytes() == before                    # Kairo may only read the status folder
    _write(note, {"message": "Kairo updated to 2026.10.16.", "ok": True, "at": "2026-10-16T03:00:00", "notified": False})
    hk.update_notice()
    assert sent[-1] == "Kairo updated to 2026.10.16."

    why = ("Kairo 2026.10.17.1100-abc1234 didn't install (try 2): the voice folder could not be built: pip failed "
           "installing onnxruntime-gpu==1.24.4 (No space left on device). Still on 2026.10.16.1104-7a7a554; next try in 30 minutes.")
    _write(note, {"message": why, "ok": False, "at": "2026-10-17T11:00:00", "notified": False})
    hk.update_notice()
    assert sent[-1] == why                                # the reason is never cut off (owner's rule 2026-10-17)


def test_voice_models_move_next_to_the_packages_once_complete(tmp_path):
    data, site = tmp_path / "data", tmp_path / "tools" / "voice-abc"
    assert models_dir(data, site) == data / "models"          # not there yet: the old place
    (site / "models").mkdir(parents=True)
    assert models_dir(data, site) == data / "models"          # half downloaded: still the old place
    (site / "models" / COMPLETE).write_text("ok", encoding="ascii")
    assert models_dir(data, site) == site / "models"
    assert models_dir(data, None) == data / "models"


def test_the_installer_downloads_voice_models_where_it_says(tmp_path, monkeypatch):
    """python -m app.voice download --to <tools\\voice-<hash>\\models>: never data\\ (fix F24)."""
    from app.voice import Voice
    from app.voice import __main__ as voice_main
    seen = {}
    monkeypatch.setattr(Voice, "download", lambda self: seen.setdefault("dir", self.models_dir) and [])
    target = tmp_path / "tools" / "voice-abc" / "models"
    assert voice_main.main(["download", "--to", str(target)]) == 0
    assert seen["dir"] == target
