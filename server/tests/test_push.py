from datetime import datetime

from fastapi.testclient import TestClient

from app.main import create_app
from app.profile import load_profile
from app.push import in_quiet_hours
from app.settings import REPO_ROOT

SUB = {"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh": "BPk", "auth": "au"}, "device": "iphone"}


def test_quiet_hours_cross_midnight():
    p = load_profile(REPO_ROOT / "config")  # 23:00-06:00
    at = lambda h, m=0: datetime(2026, 10, 13, h, m, tzinfo=p.tz)  # noqa: E731
    assert in_quiet_hours(p, at(23, 30)) and in_quiet_hours(p, at(5, 59))
    assert not in_quiet_hours(p, at(6, 0)) and not in_quiet_hours(p, at(22, 59))


def test_subscribe_and_new_card_pushes(migrated, monkeypatch):
    sent = []
    app = create_app(migrated)
    pusher = app.state.pusher
    pusher._send = lambda **kw: sent.append(kw)
    monkeypatch.setattr("app.push.in_quiet_hours", lambda *a, **k: False)
    with TestClient(app) as c:
        key = c.get("/api/push/key").json()["publicKey"]
        assert len(key) > 80
        assert c.post("/api/push/subscribe", json=SUB).status_code == 200
        assert c.post("/api/push/subscribe", json=SUB).status_code == 200   # same device twice = one row
        assert c.post("/api/push/subscribe", json={**SUB, "endpoint": "http://evil"}).status_code == 400
        pusher.notify("#4 Reply to Mr. Keller", "Approve?", "/#approvals", wait=True)
    assert len(sent) == 1
    assert sent[0]["subscription_info"]["endpoint"] == SUB["endpoint"]
    assert "#4 Reply" in sent[0]["data"]


def test_quiet_hours_suppress_push(migrated, monkeypatch):
    sent = []
    app = create_app(migrated)
    app.state.pusher._send = lambda **kw: sent.append(kw)
    monkeypatch.setattr("app.push.in_quiet_hours", lambda *a, **k: True)
    with TestClient(app) as c:
        c.post("/api/push/subscribe", json=SUB)
        app.state.pusher.notify("x", "y", wait=True)
    assert sent == []


def test_gone_devices_are_removed(migrated, monkeypatch):
    class Gone(Exception):
        response = type("R", (), {"status_code": 410})()

    def boom(**kw):
        raise Gone()

    app = create_app(migrated)
    app.state.pusher._send = boom
    monkeypatch.setattr("app.push.in_quiet_hours", lambda *a, **k: False)
    with TestClient(app) as c:
        c.post("/api/push/subscribe", json=SUB)
        app.state.pusher.notify("x", "y", wait=True)
        assert c.get("/api/push/devices").json() == []
