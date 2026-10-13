import json

from fastapi.testclient import TestClient

from app.main import create_app
import app.main as main_mod


def local(monkeypatch):
    monkeypatch.setattr(main_mod, "LOCAL_HOSTS", {"testclient", "127.0.0.1"})


def test_selftest_runs_one_brain_call(migrated, monkeypatch):
    local(monkeypatch)
    with TestClient(create_app(migrated), base_url="http://127.0.0.1:8000") as c:
        r = c.post("/api/selftest")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["brain"] == "ok"


def test_selftest_reports_logged_out(migrated, monkeypatch):
    local(monkeypatch)
    monkeypatch.setenv("KAIRO_HOOK_FAKE_MODE", "logged_out")
    with TestClient(create_app(migrated), base_url="http://127.0.0.1:8000") as c:
        body = c.post("/api/selftest").json()
    assert body["ok"] is False and body["brain"] == "logged_out"


def test_selftest_not_reachable_through_tailscale(migrated, monkeypatch):
    local(monkeypatch)
    with TestClient(create_app(migrated), base_url="https://kairo-home.tail1234.ts.net") as c:
        assert c.post("/api/selftest").status_code == 403
    with TestClient(create_app(migrated), base_url="http://127.0.0.1:8000") as c:
        assert c.post("/api/selftest", headers={"x-forwarded-for": "100.64.0.2"}).status_code == 403


def test_status_shows_last_boot_check(migrated):
    with TestClient(create_app(migrated)) as c:
        assert c.get("/api/status").json()["boot_check"] is None
        (migrated.data_dir / "logs").mkdir(parents=True, exist_ok=True)
        (migrated.data_dir / "logs" / "boot-check.json").write_text(json.dumps({"all_ok": True}), encoding="utf-8")
        assert c.get("/api/status").json()["boot_check"] == {"all_ok": True}
