"""Google sign-in (PKCE, loopback) and the read/write token split."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.main import create_app
from app.profile import load_profile
from app.settings import REPO_ROOT
from tests.fakes.fake_google import FakeGoogle

OWNER = load_profile(REPO_ROOT / "config").gmail


@pytest.fixture
def setup(migrated, monkeypatch):
    monkeypatch.setattr(main_mod, "LOCAL_HOSTS", {"testclient", "127.0.0.1"})
    secrets = migrated.data_dir / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / "google-client.json").write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))
    fake = FakeGoogle(owner=OWNER)
    app = create_app(migrated)
    app.state.oauth.transport = fake.transport()
    client = TestClient(app, base_url="http://127.0.0.1:8000", follow_redirects=False)
    yield client, fake, secrets
    client.close()


def start(client, kind):
    r = client.get(f"/setup/google/start?kind={kind}")
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    return q


def test_start_uses_pkce_offline_consent_and_the_right_scopes(setup):
    client, _, _ = setup
    q = start(client, "read")
    assert q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["redirect_uri"] == ["http://127.0.0.1:8000/setup/google/callback"]
    assert set(q["scope"][0].split()) == {"https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/calendar.readonly"}
    assert q["login_hint"] == [OWNER]
    w = start(client, "write")
    assert "https://www.googleapis.com/auth/gmail.compose" in w["scope"][0] and "readonly" not in w["scope"][0]


def test_full_sign_in_saves_token_for_that_kind_only(setup):
    client, fake, secrets = setup
    state = start(client, "read")["state"][0]
    r = client.get(f"/setup/google/callback?state={state}&code=good-code")
    assert r.status_code == 200 and OWNER in r.text
    token = json.loads((secrets / "google-read.json").read_text())
    assert token["refresh_token"] == "rt-new" and token["email"] == OWNER
    assert not (secrets / "google-write.json").exists()
    assert client.get("/api/setup/google/status").json()["read"] == OWNER


def test_wrong_account_is_refused(setup):
    client, fake, secrets = setup
    fake.owner = "someone.else@gmail.com"
    state = start(client, "write")["state"][0]
    r = client.get(f"/setup/google/callback?state={state}&code=good-code")
    assert r.status_code == 400 and "someone.else@gmail.com" in r.text
    assert not (secrets / "google-write.json").exists()


def test_missing_permissions_are_refused(setup):
    client, fake, secrets = setup
    fake.grant_scope = "https://www.googleapis.com/auth/gmail.readonly"
    state = start(client, "read")["state"][0]
    assert client.get(f"/setup/google/callback?state={state}&code=good-code").status_code == 400
    assert not (secrets / "google-read.json").exists()


def test_state_is_single_use_and_must_be_known(setup):
    client, _, _ = setup
    state = start(client, "read")["state"][0]
    assert client.get(f"/setup/google/callback?state={state}&code=good-code").status_code == 200
    assert client.get(f"/setup/google/callback?state={state}&code=good-code").status_code == 400
    assert client.get("/setup/google/callback?state=forged&code=good-code").status_code == 400


def test_sign_in_from_elsewhere_only_via_the_tailnet_name(setup, migrated):
    """Off the PC only through Kairo's tailnet address, and only with the phone client (test_google_relogin)."""
    _, _, _ = setup
    remote = TestClient(create_app(migrated), base_url="https://kairo-home.tail1234.ts.net", follow_redirects=False)
    assert remote.get("/setup/google/start?kind=read").status_code == 400          # no phone client yet
    assert remote.get("/setup/google/callback?state=x&code=y").status_code == 400  # unknown state
    other = TestClient(create_app(migrated), base_url="http://192.168.1.20:8000", follow_redirects=False)
    assert other.get("/setup/google/start?kind=read").status_code == 403
    assert other.get("/setup/google/callback?state=x&code=y").status_code == 403


def test_no_client_file_gives_a_plain_message(setup):
    client, _, secrets = setup
    (secrets / "google-client.json").unlink()
    r = client.get("/setup/google/start?kind=read")
    assert r.status_code == 400 and "not set up" in r.text.lower()
