"""Google sign-in again from the iPhone (Testing mode logs out every 7 days),
and the reminder a day before."""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app import housekeeping
from app.main import create_app
from app.profile import load_profile
from app.settings import REPO_ROOT
from tests.fakes.fake_google import FakeGoogle, connect

OWNER = load_profile(REPO_ROOT / "config").gmail
TS = "https://kairo-home.tail1234.ts.net"
DAY = 86400


@pytest.fixture
def app_and_fake(migrated, monkeypatch):
    monkeypatch.setattr(main_mod, "LOCAL_HOSTS", {"127.0.0.1"})
    secrets = migrated.data_dir / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / "google-client.json").write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))
    fake = FakeGoogle(owner=OWNER)
    app = create_app(migrated)
    app.state.oauth.transport = fake.transport()
    return app, fake, secrets


def web_client(secrets):
    (secrets / "google-web-client.json").write_text(json.dumps({"web": {"client_id": "web-cid", "client_secret": "wcs"}}))


def test_phone_sign_in_needs_the_phone_client(app_and_fake):
    app, _, _ = app_and_fake
    with TestClient(app, base_url=TS, follow_redirects=False) as c:
        r = c.get("/setup/google/start?kind=read")
    assert r.status_code == 400 and "Kairo Logins" in r.text


def test_phone_sign_in_uses_the_tailnet_address(app_and_fake):
    app, fake, secrets = app_and_fake
    web_client(secrets)
    with TestClient(app, base_url=TS, follow_redirects=False) as c:
        r = c.get("/setup/google/start?kind=read")
        assert r.status_code == 302
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["redirect_uri"] == [f"{TS}/setup/google/callback"] and q["client_id"] == ["web-cid"]
        done = c.get(f"/setup/google/callback?state={q['state'][0]}&code=good-code")
    assert done.status_code == 200 and "write" in done.text   # offers step 2 of 2
    token = json.loads((secrets / "google-read.json").read_text())
    assert token["email"] == OWNER and token["issued_at"] > time.time() - 60
    exchange = [c for c in fake.calls if c["path"] == "/token" and c["body"].get("grant_type") == ["authorization_code"]][-1]
    assert exchange["body"]["redirect_uri"] == [f"{TS}/setup/google/callback"] and exchange["body"]["client_id"] == ["web-cid"]


def test_phone_sign_in_only_on_the_tailnet_name(app_and_fake):
    app, _, secrets = app_and_fake
    web_client(secrets)
    with TestClient(app, base_url="https://evil.example.com", follow_redirects=False) as c:
        assert c.get("/setup/google/start?kind=read").status_code == 403


def test_a_pc_sign_in_cannot_be_finished_from_the_phone(app_and_fake, monkeypatch):
    app, _, secrets = app_and_fake
    web_client(secrets)
    monkeypatch.setattr(main_mod, "LOCAL_HOSTS", {"testclient", "127.0.0.1"})
    with TestClient(app, base_url="http://127.0.0.1:8000", follow_redirects=False) as c:
        state = parse_qs(urlparse(c.get("/setup/google/start?kind=read").headers["location"]).query)["state"][0]
    monkeypatch.setattr(main_mod, "LOCAL_HOSTS", {"127.0.0.1"})
    with TestClient(app, base_url=TS) as c:
        assert c.get(f"/setup/google/callback?state={state}&code=good-code").status_code == 400
    assert not (secrets / "google-read.json").exists()


# ---------------------------------------------------------------- reminders

def _age(secrets, kind, seconds):
    p = secrets / f"google-{kind}.json"
    t = json.loads(p.read_text())
    t["issued_at"] = time.time() - seconds
    p.write_text(json.dumps(t))


def test_reminder_a_day_before_the_7_days_end(app_and_fake, monkeypatch):
    app, fake, secrets = app_and_fake
    connect(secrets, fake)
    sent = []
    monkeypatch.setattr(app.state.pusher, "notify", lambda title, body, url="/", **k: sent.append((title, body, url)))
    hk = housekeeping.Housekeeping(app)
    _age(secrets, "read", 5 * DAY)
    _age(secrets, "write", 5 * DAY)
    hk.google_reminder()
    assert sent == []
    _age(secrets, "read", 6 * DAY + 60)
    hk.google_reminder()
    hk.google_reminder()
    assert len(sent) == 1 and "tomorrow" in sent[0][1] and sent[0][2] == "/#more"


def test_signed_out_push_once(app_and_fake, monkeypatch):
    app, fake, secrets = app_and_fake
    connect(secrets, fake)
    _age(secrets, "read", 7 * DAY + 2 * 3600)
    fake.revoked = True
    sent = []
    monkeypatch.setattr(app.state.pusher, "notify", lambda title, body, url="/", **k: sent.append((title, body, url)))
    app.state.google_transport = fake.transport()
    hk = housekeeping.Housekeeping(app)
    hk.google_reminder()
    hk.google_reminder()
    assert len(sent) == 1 and "signed Kairo out" in sent[0][1]


def test_no_reminder_once_tokens_outlive_7_days(app_and_fake, monkeypatch):
    """Published ("In production") apps keep their tokens: no more reminders."""
    app, fake, secrets = app_and_fake
    connect(secrets, fake)
    _age(secrets, "read", 8 * DAY)
    _age(secrets, "write", 8 * DAY)
    app.state.google_transport = fake.transport()
    sent = []
    monkeypatch.setattr(app.state.pusher, "notify", lambda title, body, url="/", **k: sent.append(title))
    hk = housekeeping.Housekeeping(app)
    hk.google_reminder()
    assert sent == []
    assert json.loads((secrets / "google-read.json").read_text())["no_expiry"] is True


@pytest.fixture(autouse=True)
def daytime(monkeypatch):
    monkeypatch.setattr("app.housekeeping.in_quiet_hours", lambda *a, **k: False)
