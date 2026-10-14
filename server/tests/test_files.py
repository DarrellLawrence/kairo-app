"""v8 B: shared files live only on kairo-home, under vault/Files/<category>/, original name kept,
filed once, never synced to the iPhone (Files/ and anything over 20 MB)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import capture, files
from app.db import Capture, make_engine
from app.main import create_app


@pytest.mark.parametrize("name, safe", [
    ("Rechnung Swisscom.pdf", "Rechnung Swisscom.pdf"),
    ("../../Windows/evil.exe", "evil.exe"),
    ("C:\\Users\\x\\Bewerbung.docx", "Bewerbung.docx"),
    ('a<b>c:"d|e?f*.txt', "a b c d e f .txt"),
    ("CON.txt", "fallback.bin"),
    ("", "fallback.bin"),
    ("x" * 300 + ".pdf", "x" * 116 + ".pdf"),
])
def test_names_are_safe_on_windows_and_linux(name, safe):
    assert files.safe_name(name, "fallback.bin") == safe


def test_never_overwrites(tmp_path):
    vault = tmp_path / "vault"
    for _ in range(3):
        src = tmp_path / "in.pdf"
        src.write_bytes(b"%PDF")
        files.place(vault, "money", "Rechnung.pdf", src)
    assert sorted(p.name for p in (vault / "Files" / "Money").iterdir()) == ["Rechnung (2).pdf", "Rechnung (3).pdf", "Rechnung.pdf"]
    assert files.category("nonsense") == "Other" and files.category("school") == "School"


def test_stignore_keeps_files_and_big_ones_off_the_phone_and_the_owners_lines(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "40 School").mkdir(parents=True)
    (vault / ".stignore").write_text("// mine\n*.tmp\n", encoding="utf-8")
    monkeypatch.setattr(files, "SYNC_MAX_BYTES", 10)
    (vault / "40 School" / "big lecture.mp4").write_bytes(b"x" * 11)
    (vault / "40 School" / "small.md").write_text("hi", encoding="utf-8")
    assert files.ensure_stignore(vault) == ["/40 School/big lecture.mp4"]
    files.ensure_stignore(vault)   # idempotent: one managed block
    text = (vault / ".stignore").read_text(encoding="utf-8")
    assert text.startswith("// mine\n*.tmp\n") and text.count(files.STIGNORE_BEGIN) == 1 and "\n/Files\n" in text
    assert (vault / files.LARGE_LIST).read_text(encoding="utf-8") == "/40 School/big lecture.mp4\n"


def test_the_shortcut_gets_kairos_one_line(migrated):
    app = create_app(migrated)

    def file_it(*a, **k):   # what the capture job does, synchronously here
        with Session(make_engine(migrated.db_url)) as s:
            for c in s.query(Capture).filter(Capture.status != "filed").all():
                c.status, c.result_json = "filed", '{"line": "Filed Rechnung.pdf in Files/Money/."}'
                s.add(c)
            s.commit()
    app.state.jobs.run_job = file_it
    with TestClient(app) as client:
        h = {"Authorization": f"Bearer {capture.token(migrated.secrets_dir)}", "Content-Type": "application/pdf",
             "X-Kairo-Filename": "Rechnung.pdf"}
        r = client.post("/api/capture?wait=1&source=share", content=b"%PDF-1.4 bill", headers=h)
        assert r.json()["message"] == "Filed Rechnung.pdf in Files/Money/."
        r = client.post("/api/capture?source=share", content=b"%PDF-1.4 other", headers=h)   # no wait: answer at once
        assert r.json()["message"].startswith("Got it.")
