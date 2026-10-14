"""Lerndokumentation: Friday push, notes -> German draft in the vault, edit, approve."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import lerndoku
from app.chat import AskResult
from app.main import create_app

NOTES = "Diese Woche habe ich im Lager Paletten kommissioniert und den Stapler-Kurs gemacht. Problem: Scanner defekt, ich habe die Liste von Hand geführt."


@pytest.fixture
def app_(migrated):
    app = create_app(migrated)
    prompts, pushes = [], []

    def ask(prompt, system):
        prompts.append(prompt)
        return AskResult(json.dumps({"title": "Kommissionieren", "markdown": "## Auftrag / Ziel\nPaletten kommissionieren.\n\n## Probleme und Lösungen\nScanner defekt: Liste von Hand."}))

    app.state.jobs._ask = ask
    app.state.pusher.notify = lambda *a, **k: pushes.append(a)
    app.prompts, app.pushes = prompts, pushes
    return app


def test_weeks():
    import datetime as dt
    assert lerndoku.week_of(dt.date(2026, 10, 16)) == "2026-W42"
    assert lerndoku.week_range("2026-W42") == (dt.date(2026, 10, 12), dt.date(2026, 10, 16))


def test_friday_push_then_draft_edit_approve(app_, migrated):
    with TestClient(app_) as c:
        run = app_.state.jobs.run_job("lerndoku_prompt", forced=True)
        assert run.status == "ok" and app_.pushes[-1][0] == "Lerndokumentation"
        app_.state.jobs.run_job("lerndoku_prompt", forced=True)   # once per week only... while waiting it may remind again
        week = run.output_path
        cur = c.get("/api/lerndoku").json()["current"]
        assert cur["week"] == week and cur["status"] == "waiting"

        assert c.post(f"/api/lerndoku/{week}/notes", json={"notes": "kurz"}).status_code == 400
        d = c.post(f"/api/lerndoku/{week}/notes", json={"notes": NOTES}).json()
        assert d["status"] == "draft" and "Entwurf" in d["draft"] and "Scanner defekt" in d["draft"]
        assert "German" in app_.prompts[-1] and NOTES in app_.prompts[-1] and "never invent" in app_.prompts[-1]
        path = migrated.data_dir / "vault" / d["vault_path"]
        assert path.read_text(encoding="utf-8").startswith("<!-- kairo:lerndoku -->")

        # The owner adds their own text outside Kairo's section; Kairo's edits keep it.
        path.write_text(path.read_text(encoding="utf-8") + "\nMeine Notiz.\n", encoding="utf-8")
        edited = d["draft"].replace("Paletten kommissionieren.", "Paletten für Kunde X kommissionieren.")
        c.put(f"/api/lerndoku/{week}", json={"draft": edited})
        a = c.post(f"/api/lerndoku/{week}/approve", json={}).json()
        assert a["status"] == "approved" and "Freigegeben am" in a["draft"] and "Kunde X" in a["draft"]
        assert "Meine Notiz." in path.read_text(encoding="utf-8")
        assert c.put(f"/api/lerndoku/{week}", json={"draft": "x"}).status_code == 400   # approved = locked
        r = c.post(f"/api/lerndoku/{week}/approve", json={"approved": False}).json()
        assert r["status"] == "draft" and "Entwurf" in r["draft"]
        assert c.get("/api/lerndoku?week=bad").status_code == 400
        assert [w["week"] for w in c.get("/api/lerndoku").json()["weeks"]] == [week]
