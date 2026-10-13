from fastapi.testclient import TestClient

from app.main import create_app


def test_health_ok_on_migrated_db(migrated):
    with TestClient(create_app(migrated)) as client:
        r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["migrated"] is True
    assert body["brain"] == "ok"


def test_health_fails_on_unmigrated_db(settings):
    with TestClient(create_app(settings)) as client:
        r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["detail"]["migrated"] is False


def test_health_reports_missing_brain(migrated):
    migrated.claude_bin = "/nonexistent/claude"
    with TestClient(create_app(migrated)) as client:
        assert client.get("/health").json()["brain"] == "unavailable"
