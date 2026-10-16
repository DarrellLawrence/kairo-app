"""Owner's decision 2026-10-15: the backend's own outbound calls (Web Push to the owner's devices,
the health watch's tailnet check; later the heartbeat and the offsite backup) don't go through the
gate, but only to destinations fixed in code. No model input, mail, web page or file can change
where they go. Written before app/outbound.py."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from sqlmodel import Session

from app import health, outbound
from app.db import PushSubscription, make_engine
from app.push import Pusher
from app.settings import REPO_ROOT
from tests.test_pa import PROFILE


@pytest.mark.parametrize("url, ok", [
    ("https://web.push.apple.com/QGu8q", True),                 # Safari / the iPhone PWA
    ("https://fcm.googleapis.com/fcm/send/abc", True),           # Chrome
    ("https://updates.push.services.mozilla.com/wpush/v2/x", True),
    ("https://wns2-am3p.notify.windows.com/w/?token=x", True),   # Edge
    ("http://web.push.apple.com/x", False),                       # not https
    ("https://web.push.apple.com.evil.example/x", False),
    ("https://evil.example/web.push.apple.com", False),
    ("https://user@web.push.apple.com:8443/x", False),            # no userinfo, no other port
    ("https://notify.windows.com.evil.example/x", False),
    ("", False), (None, False),
])
def test_push_endpoints_only_on_the_push_services(url, ok):
    assert outbound.push_endpoint_ok(url) is ok


@pytest.mark.parametrize("url, ok", [
    ("https://kairo-home.tail1234.ts.net", True),
    ("https://kairo-home.tail1234.ts.net/", True),
    ("http://kairo-home.tail1234.ts.net", False),
    ("https://evil.example", False),
    ("https://ts.net.evil.example", False),
    ("https://kairo-home.tail1234.ts.net.evil.example", False),
    ("https://user@kairo-home.tail1234.ts.net", False),
    (None, False),
])
def test_the_health_check_only_calls_kairos_tailnet_address(url, ok):
    assert outbound.tailnet_url_ok(url) is ok


def _pusher(migrated, sent):
    return Pusher(engine=make_engine(migrated.db_url), secrets_dir=migrated.data_dir / "secrets", profile=PROFILE,
                  sender=lambda **kw: sent.append(kw["subscription_info"]["endpoint"]))


def test_subscribe_refuses_other_hosts(migrated):
    p = _pusher(migrated, [])
    with pytest.raises(ValueError):
        p.subscribe("https://evil.example/push", {"p256dh": "k", "auth": "a"}, "phone")
    p.subscribe("https://web.push.apple.com/ok", {"p256dh": "k", "auth": "a"}, "phone")


def test_a_stored_row_with_another_host_is_never_sent_to(migrated):
    sent: list[str] = []
    p = _pusher(migrated, sent)
    with Session(p.engine) as s:
        for ep in ("https://evil.example/push", "https://web.push.apple.com/ok"):
            s.add(PushSubscription(endpoint=ep, keys_json=json.dumps({"p256dh": "k", "auth": "a"})))
        s.commit()
    p._send_all("Kairo", "hello", "/")
    assert sent == ["https://web.push.apple.com/ok"]


def test_health_ignores_a_planted_address(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "boot-check.json").write_text(json.dumps({"kairo_url": "https://evil.example/x"}), encoding="utf-8")
    assert health.kairo_url(tmp_path) is None
    (tmp_path / "logs" / "boot-check.json").write_text(json.dumps({"kairo_url": "https://kairo-home.tail1.ts.net"}), encoding="utf-8")
    assert health.kairo_url(tmp_path) == "https://kairo-home.tail1.ts.net"


def test_only_known_modules_talk_to_the_network():
    """A new outbound path must be reviewed (and added to outbound.MODULES with its fixed hosts)."""
    app = REPO_ROOT / "server" / "app"
    found = set()
    for f in app.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        if any(k in text for k in ("import httpx", "urllib.request", "import requests", "pywebpush", "http.client",
                                   "import websockets", "from websockets", "aiortc", "aioice", "smallwebrtc")):
            found.add(f.relative_to(app).as_posix())
    assert found == set(outbound.MODULES), sorted(found ^ set(outbound.MODULES))


def test_webrtc_calls_use_the_tailnet_only():
    """Calls over WebRTC: this PC offers only its tailnet addresses and asks no STUN/TURN server."""
    pytest.importorskip("aioice")
    import aioice.ice

    from app import voicertc
    before = aioice.ice.get_host_addresses
    try:
        aioice.ice.get_host_addresses = lambda use_ipv4, use_ipv6: ["192.168.1.20", "100.70.1.2", "85.1.2.3",
                                                                    "fd7a:115c:a1e0::5", "2a02:1210::1"]
        voicertc.tailnet_only()
        assert aioice.ice.get_host_addresses(True, True) == ["100.70.1.2", "fd7a:115c:a1e0::5"]
    finally:
        aioice.ice.get_host_addresses = before
    src = (REPO_ROOT / "server" / "app" / "voicertc.py").read_text(encoding="utf-8")
    assert voicertc.ALLOW_LOOPBACK is False and not any(
        "ALLOW_LOOPBACK = True" in f.read_text(encoding="utf-8") for f in (REPO_ROOT / "server" / "app").rglob("*.py"))
    assert "ice_servers=[]" in src and not re.search(r"[\'\"](stun|turns?):", src)


def test_realtime_voice_only_talks_to_openai():
    """Owner's decision 2026-10-15: the realtime voice layer reaches OpenAI's API and nothing else."""
    from app import realtime
    assert set(realtime.HOSTS.values()) == {"api.openai.com", "eu.api.openai.com"}
    assert "realtime.py" in outbound.MODULES
