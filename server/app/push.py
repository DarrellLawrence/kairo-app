"""Web Push to the owner's devices (KICKOFF 4, 11).

VAPID keys are made on first start and kept in <data>/secrets. Devices
subscribe from the Kairo app (only reachable over the tailnet). No pushes in
the quiet hours from profile.yaml; the cards still wait in the app.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Callable

from sqlmodel import Session, select

from .db import PushSubscription
from .profile import Profile

log = logging.getLogger("kairo.push")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def ensure_vapid(secrets_dir: Path) -> tuple[Path, str]:
    """Returns (private key PEM path, public key for the browser)."""
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid01

    secrets_dir.mkdir(parents=True, exist_ok=True)
    pem = secrets_dir / "vapid-private.pem"
    if not pem.is_file():
        v = Vapid01()
        v.generate_keys()
        pem.write_bytes(v.private_pem())
    v = Vapid01.from_file(str(pem))
    public = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return pem, _b64url(public)


def in_quiet_hours(profile: Profile, now: datetime | None = None) -> bool:
    now = (now or datetime.now(profile.tz)).astimezone(profile.tz)
    start = dtime.fromisoformat(profile.quiet_hours.start)
    end = dtime.fromisoformat(profile.quiet_hours.end)
    t = now.time()
    return (start <= t or t < end) if start > end else (start <= t < end)


class Pusher:
    def __init__(self, *, engine, secrets_dir: Path, profile: Profile, sender: Callable | None = None):
        self.engine = engine
        self.profile = profile
        self.pem, self.public_key = ensure_vapid(Path(secrets_dir))
        if sender is None:
            from pywebpush import webpush as sender
        self._send = sender

    def subscribe(self, endpoint: str, keys: dict, device: str | None) -> None:
        if not endpoint.startswith("https://") or not {"p256dh", "auth"} <= set(keys):
            raise ValueError("not a push subscription")
        with Session(self.engine) as s:
            row = s.exec(select(PushSubscription).where(PushSubscription.endpoint == endpoint)).first()
            row = row or PushSubscription(endpoint=endpoint, keys_json="{}")
            row.keys_json = json.dumps({"p256dh": keys["p256dh"], "auth": keys["auth"]})
            row.device = (device or "")[:100] or None
            s.add(row)
            s.commit()

    def notify(self, title: str, body: str, url: str = "/", *, ignore_quiet: bool = False, wait: bool = False) -> None:
        if not ignore_quiet and in_quiet_hours(self.profile):
            log.info("quiet hours: no push for %r", title)
            return
        worker = threading.Thread(target=self._send_all, args=(title, body, url), daemon=True)
        worker.start()
        if wait:
            worker.join(timeout=30)

    def _send_all(self, title: str, body: str, url: str) -> None:
        payload = json.dumps({"title": title, "body": body[:240], "url": url})
        claims = {"sub": f"mailto:{self.profile.gmail or 'kairo@localhost'}"}
        with Session(self.engine) as s:
            for sub in s.exec(select(PushSubscription)).all():
                info = {"endpoint": sub.endpoint, "keys": json.loads(sub.keys_json)}
                try:
                    self._send(subscription_info=info, data=payload, vapid_private_key=str(self.pem),
                               vapid_claims=dict(claims), timeout=10)
                except Exception as exc:  # pywebpush raises WebPushException with .response
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status in (404, 410):
                        s.delete(sub)   # the device unsubscribed
                        s.commit()
                    log.warning("push failed (%s): %s", status, exc)
