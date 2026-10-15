"""Where the backend's own outbound calls may go (owner's decision 2026-10-15).

The gate checks everything the model asks for. The backend's own calls carry nothing the model
chose and don't go through the gate, so their destinations are fixed here instead: no model
input, mail, web page, file or setting can point them anywhere else.

- Web Push to the owner's devices: only the browsers' push services.
- Health watch: only Kairo's own tailnet address (https://<name>.ts.net).
- Google: only Google's API hosts (google/client.py, google/oauth.py, executor.py).
- Weather and stations: lookup.ALLOWED_HOSTS.
- Model downloads (installer/first start): fixed URLs in semantic.py and voice/__init__.py.
- Realtime voice (owner's decision 2026-10-15, voice only): only OpenAI's API (realtime.HOSTS).

MODULES lists every module that may talk to the network; a test fails when another one does.
"""

from __future__ import annotations

from urllib.parse import urlsplit

PUSH_HOSTS = {"web.push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com"}
PUSH_HOST_SUFFIXES = (".notify.windows.com",)        # Edge (WNS): wns2-<region>.notify.windows.com
TAILNET_SUFFIX = ".ts.net"

MODULES = {
    "executor.py": "Google writes (after the gate)",
    "google/client.py": "Google reads",
    "google/oauth.py": "Google sign-in",
    "health.py": "Kairo's own tailnet address (tailnet_url_ok)",
    "housekeeping.py": "Google token check (via google/client.py)",
    "lookup.py": "weather and stations (lookup.ALLOWED_HOSTS)",
    "push.py": "Web Push to the owner's devices (push_endpoint_ok)",
    "realtime.py": "realtime voice: OpenAI's API only (realtime.HOSTS)",
    "semantic.py": "the memory model download (fixed host)",
    "voice/__init__.py": "the voice model download (fixed URLs)",
}


def _plain_https_host(url) -> str | None:
    """The host of a plain https URL (no user info, default port), else None."""
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.username or parts.password or port not in (None, 443) or not parts.hostname:
        return None
    return parts.hostname.lower()


def push_endpoint_ok(url) -> bool:
    host = _plain_https_host(url)
    return bool(host) and (host in PUSH_HOSTS or host.endswith(PUSH_HOST_SUFFIXES))


def tailnet_url_ok(url) -> bool:
    host = _plain_https_host(url)
    return bool(host) and host.endswith(TAILNET_SUFFIX) and host.count(".") >= 2
