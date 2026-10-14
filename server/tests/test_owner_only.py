"""Owner only, app-wide (owner's request 2026-10-14): every route and the chat websocket answer only
the PC itself or the PC's own Tailscale account through Kairo's tailnet address. /health stays open:
it's what the boot check and the updater poll, and it holds nothing personal."""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

import app.main as main_mod
from app.main import create_app

TAILNET = "https://kairo-home.tail1234.ts.net"
OWNER = {"tailscale-user-login": "Owner@Example.com"}
GUEST = {"tailscale-user-login": "guest@example.com"}
OPEN = {"/health"}
WS_HOST = {"host": "kairo-home.tail1234.ts.net"}          # TestClient's websockets ignore base_url


@pytest.fixture
def strict(migrated, monkeypatch):
    monkeypatch.setattr(main_mod, "LOCAL_HOSTS", {"testclient", "127.0.0.1"})
    return migrated.model_copy(update={"owner_check": True})


def pin(settings, login: str) -> None:
    d = settings.data_dir / "logs"
    d.mkdir(parents=True, exist_ok=True)
    (d / "boot-check.json").write_text(json.dumps({"all_ok": True, "checks": [], "tailnet_login": login}), encoding="utf-8")


def test_the_owner_gets_in(strict):
    with TestClient(create_app(strict), base_url="http://127.0.0.1:8000") as c:
        assert c.get("/api/status").status_code == 200                     # on the PC
    with TestClient(create_app(strict), base_url=TAILNET) as c:
        assert c.get("/api/status", headers=OWNER).status_code == 200      # before the boot check pinned the account
        pin(strict, "owner@example.com")
        assert c.get("/api/status", headers=OWNER).status_code == 200
        with c.websocket_connect("/ws/chat", headers=OWNER | WS_HOST):
            pass


def test_nobody_else(strict):
    pin(strict, "owner@example.com")
    with TestClient(create_app(strict), base_url=TAILNET) as c:
        assert c.get("/api/status").status_code == 403                     # tagged device: no person
        assert c.get("/api/status", headers=GUEST).status_code == 403      # another tailnet account
        assert c.get("/", headers=GUEST).status_code == 403                # the app shell too
        assert c.get("/health", headers=GUEST).status_code == 200          # open on purpose
        with pytest.raises(WebSocketDisconnect) as e, c.websocket_connect("/ws/chat", headers=GUEST | WS_HOST):
            pass
        assert e.value.code == 1008
    with TestClient(create_app(strict), base_url="http://kairo.example.org") as c:
        assert c.get("/api/status", headers=OWNER).status_code == 403      # not Kairo's tailnet address
    with TestClient(create_app(strict), base_url="http://127.0.0.1:8000") as c:
        assert c.get("/api/status", headers={"x-forwarded-for": "100.64.0.2"}).status_code == 403


def test_every_route_is_owner_only(strict):
    """No route slips through: a guest gets 403 on every path the app has, except /health."""
    pin(strict, "owner@example.com")
    app = create_app(strict)
    with TestClient(app, base_url=TAILNET) as c:
        for route in app.routes:
            path = re.sub(r"\{[^}]+\}", "1", route.path)
            if isinstance(route, WebSocketRoute):
                with pytest.raises(WebSocketDisconnect), c.websocket_connect(path, headers=GUEST | WS_HOST):
                    pass
                continue
            if not isinstance(route, Route) or route.path in OPEN:
                continue
            for method in sorted(route.methods - {"HEAD"}):
                r = c.request(method, path, headers=GUEST)
                assert r.status_code == 403, (method, route.path, r.status_code)


def test_owner_check_is_on_outside_tests(monkeypatch, tmp_path):
    from app.settings import load_settings
    monkeypatch.setenv("KAIRO_DATA_DIR", str(tmp_path))
    assert load_settings().owner_check is True
