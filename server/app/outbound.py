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
- Local voice (owner's decision 2026-10-16): the voice model's server on 127.0.0.1 only
  (brain/voice_local.py); its files from GitHub (llama.cpp, checked against GitHub's sha256) and
  Hugging Face (checked against Hugging Face's sha256), downloaded by the installer (voicellm.py), the
  speaker check's model too (owner's approval 2026-10-20: one file, pinned revision and sha256).
  speaker.py itself never touches the network.
- The voice latency harness (bench_voice.py, bench_phone.py): a fake phone that calls Kairo itself, on 127.0.0.1 or its own
  tailnet address, to time the voice. Run by the owner or `kairo diag`; the model can't start it.
- Calls over WebRTC (owner's request 2026-10-16, voice only): the call's audio to the owner's phone on
  the tailnet only. This PC offers only its Tailscale addresses, takes only the phone's tailnet
  candidates and asks no STUN/TURN server (voicertc.tailnet_only, offer_on_tailnet).

- kairo diag (diag.py): one GET to Kairo's own server on 127.0.0.1 (/api/diag/voice), fixed host, no proxy.

MODULES lists every module that may talk to the network; a test fails when another one does.
"""

from __future__ import annotations

from urllib.parse import urlsplit

PUSH_HOSTS = {"web.push.apple.com", "fcm.googleapis.com", "updates.push.services.mozilla.com"}
PUSH_HOST_SUFFIXES = (".notify.windows.com",)        # Edge (WNS): wns2-<region>.notify.windows.com
TAILNET_SUFFIX = ".ts.net"

MODULES = {
    "bench_phone.py": "the voice latency harness's fake phone (WebRTC), and the mock call it talks to in this process: tailnet addresses only, no STUN/TURN",
    "bench_voice.py": "the voice latency harness: its fake phone calls Kairo's own /api/rtc/offer (127.0.0.1 or Kairo's *.ts.net address), by hand or from `kairo diag`; never the model",
    "brain/voice_local.py": "the local voice model's server on 127.0.0.1 (nothing else)",
    "diag.py": "kairo diag asks Kairo's own server on 127.0.0.1 (nothing else)",
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
    "voicellm.py": "the installer's voice-model download (GitHub llama.cpp release, Hugging Face)",
    "voicertc.py": "the call's audio to the owner's phone, tailnet addresses only (no STUN/TURN)",
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
