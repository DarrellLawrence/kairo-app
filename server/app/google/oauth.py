"""Google sign-in for kairo-home (Desktop OAuth client, loopback redirect, PKCE).

Two sign-ins, one per token kind (see client.SCOPES). Only reachable from the
PC itself (127.0.0.1): the installer opens the browser there. The signed-in
address must match profile.yaml's gmail, so a wrong account can't slip in.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .client import AUTH_URL, SCOPES, TOKEN_URL, ClientConfig, Kind, load_token, save_token

STATE_TTL_S = 900


class OAuthError(Exception):
    pass


@dataclass
class _Pending:
    kind: Kind
    verifier: str
    created: float = field(default_factory=time.time)


class GoogleOAuth:
    def __init__(self, secrets_dir: Path, redirect_uri: str, expected_email: str | None,
                 transport: httpx.BaseTransport | None = None):
        self.secrets_dir = Path(secrets_dir)
        self.redirect_uri = redirect_uri
        self.expected_email = (expected_email or "").strip().lower() or None
        self.transport = transport
        self._pending: dict[str, _Pending] = {}

    def status(self) -> dict:
        def email(kind: Kind):
            t = load_token(self.secrets_dir, kind)
            return t.get("email") if t and t.get("refresh_token") else None
        return {"client": (self.secrets_dir / "google-client.json").is_file(),
                "read": email("read"), "write": email("write"), "expected": self.expected_email}

    def start_url(self, kind: Kind) -> str:
        if kind not in SCOPES:
            raise OAuthError("kind must be read or write")
        client = ClientConfig.load(self.secrets_dir)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        state = secrets.token_urlsafe(24)
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if now - v.created < STATE_TTL_S}
        self._pending[state] = _Pending(kind=kind, verifier=verifier)
        params = {
            "client_id": client.client_id, "redirect_uri": self.redirect_uri, "response_type": "code",
            "scope": " ".join(SCOPES[kind]), "access_type": "offline", "prompt": "consent",
            "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        }
        if self.expected_email:
            params["login_hint"] = self.expected_email
        return AUTH_URL + "?" + urlencode(params)

    def finish(self, *, state: str, code: str) -> dict:
        pending = self._pending.pop(state, None)
        if pending is None or time.time() - pending.created > STATE_TTL_S:
            raise OAuthError("This sign-in link is old or unknown. Start the Google step again.")
        client = ClientConfig.load(self.secrets_dir)
        with httpx.Client(transport=self.transport, timeout=30.0) as http:
            r = http.post(TOKEN_URL, data={
                "code": code, "client_id": client.client_id, "client_secret": client.client_secret,
                "redirect_uri": self.redirect_uri, "grant_type": "authorization_code", "code_verifier": pending.verifier,
            })
            if r.status_code != 200:
                raise OAuthError(f"Google refused the sign-in ({r.status_code}).")
            data = r.json()
            granted = set((data.get("scope") or "").split())
            missing = [s for s in SCOPES[pending.kind] if s not in granted]
            if missing:
                raise OAuthError("Not all permissions were allowed. Start again and tick every box Google shows.")
            if not data.get("refresh_token"):
                raise OAuthError("Google gave no long-term token. Start again (Kairo asks with prompt=consent).")
            profile = http.get("https://www.googleapis.com/gmail/v1/users/me/profile",
                               headers={"Authorization": f"Bearer {data['access_token']}"})
            if profile.status_code != 200:
                raise OAuthError("Could not read the Gmail address of this account.")
            email = profile.json().get("emailAddress", "").lower()
        if self.expected_email and email != self.expected_email:
            raise OAuthError(f"You signed in as {email}, but Kairo is set up for {self.expected_email}. "
                             f"Start again and pick {self.expected_email}.")
        save_token(self.secrets_dir, pending.kind, {
            "refresh_token": data["refresh_token"], "access_token": data["access_token"],
            "expires_at": time.time() + int(data.get("expires_in", 3600)), "scopes": sorted(granted), "email": email,
        })
        return {"kind": pending.kind, "email": email}
