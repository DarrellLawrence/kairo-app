import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app, same_origin


def test_same_origin_rules():
    assert same_origin(None, "kairo-home.x.ts.net")
    assert same_origin("https://kairo-home.x.ts.net", "kairo-home.x.ts.net")
    assert same_origin("http://127.0.0.1:8000", "127.0.0.1:8000")
    assert not same_origin("https://evil.example", "kairo-home.x.ts.net")
    assert not same_origin("https://kairo-home.x.ts.net.evil.example", "kairo-home.x.ts.net")
    assert not same_origin("null", "kairo-home.x.ts.net")


def test_cross_origin_websocket_is_refused(migrated):
    with TestClient(create_app(migrated)) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws/chat", headers={"origin": "https://evil.example"}) as ws:
                ws.receive_json()
        assert exc.value.code == 1008


def test_same_origin_websocket_works(migrated):
    with TestClient(create_app(migrated)) as client:
        with client.websocket_connect("/ws/chat", headers={"origin": "http://testserver"}) as ws:
            ws.send_json({"type": "message", "text": "hi"})
            assert ws.receive_json()["type"] == "thread"


def test_cross_origin_post_is_refused(migrated):
    with TestClient(create_app(migrated)) as client:
        assert client.post("/api/loops", json={"kind": "task", "what": "x"}, headers={"origin": "https://evil.example"}).status_code == 403
        assert client.post("/api/loops", json={"kind": "task", "what": "x"}, headers={"origin": "http://testserver"}).status_code == 200


def test_forwarded_host_from_tailscale_serve_is_accepted():
    assert same_origin("https://kairo-home.x.ts.net", "127.0.0.1:8000", "kairo-home.x.ts.net")
    assert not same_origin("https://evil.example", "127.0.0.1:8000", "kairo-home.x.ts.net")
    assert not same_origin("https://evil.example", "127.0.0.1:8000", None)
