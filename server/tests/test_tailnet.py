"""Voice V3 (owner's request 2026-10-16): direct or through Tailscale's relay, from `tailscale status`."""

from __future__ import annotations

import json
import time

from app import tailnet

RAW = {
    "Self": {"Relay": "fra", "TailscaleIPs": ["100.64.0.1"]},
    "Peer": {
        "nodekey:a": {"TailscaleIPs": ["100.90.1.2", "fd7a:115c:a1e0::2"], "OS": "iOS", "Online": True,
                      "CurAddr": "192.168.1.30:41641", "Relay": "fra"},
        "nodekey:b": {"TailscaleIPs": ["100.90.1.3"], "OS": "iOS", "Online": True, "CurAddr": "", "Relay": "nue"},
        "nodekey:c": {"TailscaleIPs": ["100.90.1.4"], "OS": "linux", "Online": True, "CurAddr": "85.1.2.3:41641",
                      "Relay": "fra"},
    },
}


def test_status_is_reduced_to_what_a_call_needs_without_public_addresses():
    st = tailnet.reduce(RAW)
    assert st["self_relay"] == "fra" and len(st["peers"]) == 3
    assert "85.1.2.3" not in json.dumps(st) and "192.168.1.30" not in json.dumps(st)
    a = next(p for p in st["peers"] if "100.90.1.2" in p["ips"])
    assert a["direct"] is True and a["lan"] is True


def test_direct_relay_or_unknown_for_the_phones_address():
    st = {**tailnet.reduce(RAW), "at": time.time() - 30, "source": "snapshot"}
    assert tailnet.path_to(st, "100.90.1.2")["net"] == "direct"
    relayed = tailnet.path_to(st, "100.90.1.3")
    assert relayed["net"] == "relay" and relayed["via"] == "nue" and relayed["age_s"] >= 29
    far = tailnet.path_to(st, "100.90.1.4")
    assert far["net"] == "direct" and far["lan"] is False         # direct over the internet (mobile data)
    assert tailnet.path_to(st, "100.99.9.9")["net"] == "unknown"
    assert tailnet.path_to(None, "100.90.1.2")["net"] == "unknown"
    assert tailnet.path_to(st, "100.90.1.3, 127.0.0.1")["net"] == "relay"   # first hop of X-Forwarded-For


def test_the_system_snapshot_is_read_when_kairo_may_not_ask_tailscale(tmp_path, monkeypatch):
    monkeypatch.setenv("KAIRO_STATUS_DIR", str(tmp_path))
    monkeypatch.setattr(tailnet, "live", lambda: None)
    (tmp_path / "tailnet.json").write_text(json.dumps({"at": time.time(), **tailnet.reduce(RAW)}), encoding="utf-8")
    st = tailnet.status(tmp_path / "data")
    assert st["source"] == "snapshot" and tailnet.path_to(st, "100.90.1.3")["net"] == "relay"
    (tmp_path / "tailnet.json").write_text("not json", encoding="utf-8")
    assert tailnet.status(tmp_path / "data") is None
