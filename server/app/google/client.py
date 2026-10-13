"""Minimal Google client (httpx, no Google libraries).

Two tokens, from the same Desktop OAuth client (KICKOFF 7):
  read   gmail.readonly + calendar.readonly. Used by the model's read tools.
         This client refuses any request that isn't a read.
  write  gmail.modify + gmail.compose + calendar.events. Used ONLY by
         app/executor.py, after the gate allowed the action.
Tokens live in <data>/secrets/, never in the CLI's environment or workspace.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

Kind = Literal["read", "write"]

SCOPES: dict[str, list[str]] = {
    "read": [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
    ],
    "write": [
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/calendar.events",
    ],
}
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
API = "https://www.googleapis.com"


class GoogleNotConnected(Exception):
    """Google sign-in hasn't been done (or the client file is missing)."""


class GoogleError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Google API error {status}: {message}")
        self.status = status


@dataclass
class ClientConfig:
    client_id: str
    client_secret: str

    @classmethod
    def load(cls, secrets_dir: Path, name: str = "google-client.json") -> "ClientConfig":
        path = secrets_dir / name
        if not path.is_file():
            raise GoogleNotConnected("Google is not set up yet (no OAuth client). Run Kairo Logins on the PC.")
        data = json.loads(path.read_text(encoding="utf-8"))
        inner = data.get("installed") or data.get("web") or data
        return cls(client_id=inner["client_id"], client_secret=inner["client_secret"])


def token_path(secrets_dir: Path, kind: Kind) -> Path:
    return secrets_dir / f"google-{kind}.json"


def load_token(secrets_dir: Path, kind: Kind) -> dict | None:
    path = token_path(secrets_dir, kind)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_token(secrets_dir: Path, kind: Kind, token: dict) -> None:
    secrets_dir.mkdir(parents=True, exist_ok=True)
    path = token_path(secrets_dir, kind)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(token), encoding="utf-8")
    tmp.replace(path)


class GoogleAPI:
    def __init__(self, secrets_dir: Path, kind: Kind, transport: httpx.BaseTransport | None = None):
        self.secrets_dir = Path(secrets_dir)
        self.kind = kind
        self._http = httpx.Client(transport=transport, timeout=30.0)

    def close(self) -> None:
        self._http.close()

    # -- auth

    def _access_token(self) -> str:
        token = load_token(self.secrets_dir, self.kind)
        if not token or not token.get("refresh_token"):
            raise GoogleNotConnected(f"Google ({self.kind}) is not signed in yet. Run Kairo Logins on the PC.")
        if token.get("access_token") and token.get("expires_at", 0) > time.time() + 60:
            return token["access_token"]
        # A refresh token only works with the client that issued it (PC or phone client).
        name = token.get("client_file")
        name = name if name in ("google-client.json", "google-web-client.json") else "google-client.json"
        client = ClientConfig.load(self.secrets_dir, name)
        r = self._http.post(TOKEN_URL, data={
            "client_id": client.client_id, "client_secret": client.client_secret,
            "refresh_token": token["refresh_token"], "grant_type": "refresh_token",
        })
        if r.status_code in (400, 401):
            raise GoogleNotConnected(f"Google ({self.kind}) sign-in expired or was revoked. Sign in again (More -> Google).")
        if r.status_code != 200:
            raise GoogleError(r.status_code, "Google sign-in service did not answer")
        data = r.json()
        token["access_token"] = data["access_token"]
        token["expires_at"] = time.time() + int(data.get("expires_in", 3600))
        save_token(self.secrets_dir, self.kind, token)
        return token["access_token"]

    def refresh(self) -> None:
        """Ask Google for a new access token now (raises GoogleNotConnected if signed out)."""
        token = load_token(self.secrets_dir, self.kind) or {}
        token.pop("expires_at", None)
        if token:
            save_token(self.secrets_dir, self.kind, token)
        self._access_token()

    # -- requests

    def request(self, method: str, path: str, *, params: dict | None = None, json_body: Any = None) -> Any:
        method = method.upper()
        if self.kind == "read" and method != "GET":
            raise PermissionError("the read-only Google client can only read")
        r = self._http.request(method, API + path, params=params, json=json_body,
                               headers={"Authorization": f"Bearer {self._access_token()}"})
        if r.status_code >= 400:
            try:
                message = r.json().get("error", {}).get("message", r.text)
            except ValueError:
                message = r.text
            raise GoogleError(r.status_code, str(message)[:300])
        if r.status_code == 204 or not r.content:
            return {}
        return r.json()

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params={k: v for k, v in params.items() if v is not None})
