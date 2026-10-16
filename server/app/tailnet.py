"""Voice V3 (owner's request 2026-10-16): does the owner's phone reach this PC directly, or through
Tailscale's relay (DERP)? A relayed call goes phone -> relay server (Frankfurt or Nuremberg: there is
none in Switzerland) -> this PC, which adds tens of ms each way and some jitter.

WebRTC can't see it (the call always runs between the two tailnet addresses); Tailscale knows: a peer
with a current address (CurAddr) is direct, otherwise it goes through its relay region. The PC asks
`tailscale status --json` itself when it may (Kairo's own account often may not), else it reads the
snapshot the auto-update writes every 5 minutes as SYSTEM (C:\\Kairo\\status\\tailnet.json: only what's
needed, no public addresses). Nothing here talks to the network.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import status_files

SNAPSHOT = "tailnet.json"
LIVE_TIMEOUT_S = 2.0


def _exe() -> str | None:
    if os.name == "nt":
        p = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale" / "tailscale.exe"
        return str(p) if p.is_file() else None
    return shutil.which("tailscale")


def _private(addr: str) -> bool | None:
    host = addr.rsplit(":", 1)[0].strip("[]") if addr else ""
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return None


def reduce(raw: dict) -> dict:
    """`tailscale status --json` down to what a call needs (no public addresses kept)."""
    peers = []
    for p in (raw.get("Peer") or {}).values():
        if not isinstance(p, dict):
            continue
        cur = str(p.get("CurAddr") or "")
        peers.append({"ips": [str(x) for x in (p.get("TailscaleIPs") or [])][:4], "os": str(p.get("OS") or "")[:20],
                      "online": bool(p.get("Online")), "direct": bool(cur), "lan": _private(cur) if cur else None,
                      "relay": str(p.get("Relay") or "")[:10] or None, "peer_relay": bool(p.get("PeerRelay"))})
    me = raw.get("Self") or {}
    return {"self_relay": str(me.get("Relay") or "")[:10] or None, "peers": peers}


def live() -> dict | None:
    exe = _exe()
    if exe is None:
        return None
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True, timeout=LIVE_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return {**reduce(json.loads(out.stdout.decode("utf-8", "replace"))), "at": time.time(), "source": "live"}
    except ValueError:
        return None


def snapshot(data_dir: Path) -> dict | None:
    got = status_files.read_json(data_dir, SNAPSHOT)
    if not isinstance(got, dict) or "peers" not in got:
        return None
    return {**got, "source": "snapshot"}


def status(data_dir: Path) -> dict | None:
    return live() or snapshot(data_dir)


def path_to(st: dict | None, ip: str | None) -> dict:
    """How the peer with this tailnet address reaches this PC: net direct (lan: on the same network) /
    relay (via: the relay region) / unknown, and how old the answer is."""
    out = {"net": "unknown", "via": None, "lan": None, "age_s": None, "source": None}
    if not st or not ip:
        return out
    out["source"] = st.get("source")
    if isinstance(st.get("at"), (int, float)):
        out["age_s"] = max(0, int(time.time() - st["at"]))
    ip = ip.split(",")[0].strip()
    for p in st.get("peers") or []:
        if ip in (p.get("ips") or []):
            if p.get("direct"):
                out.update(net="direct", lan=p.get("lan"))
            else:
                out.update(net="relay", via="peer relay" if p.get("peer_relay") else (p.get("relay") or st.get("self_relay")))
            return out
    return out
