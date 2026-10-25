"""Web Push to the owner's devices (KICKOFF 4, 11), with one notification boss (fix plan item 20).

VAPID keys are made on first start and kept in <data>/secrets. Devices subscribe from the Kairo
app (only reachable over the tailnet). Every push goes through Pusher.notify, which decides:
- quiet hours (the profile's, the one source): held until they end, then sent as one merged push;
  a backup failure is not held: it goes to the top of the morning brief instead;
- the same push (source + text) again within 6 h: dropped;
- proactive pings (loops, coach, location, Lerndoku) share one daily budget (max_pings);
  approvals, replies people wait for, calls, security and system problems never wait for it;
- every decision is logged in push_log (source, text, sent / held / dropped, why).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Callable

from sqlmodel import Session, select

from . import outbound
from .db import PushLog, PushSubscription
from .profile import Profile

log = logging.getLogger("kairo.push")

PROACTIVE = {"loops", "coach", "location", "lerndoku"}      # share the daily ping budget
TO_BRIEF = {"backup"}                                       # at night: the morning brief shows it instead
DEDUPE = timedelta(hours=6)
DEFAULT_MAX = 6
URGENCIES = ("very-low", "low", "normal", "high")      # RFC 8030 section 5.3
MAX_TTL_S = 7 * 24 * 3600                              # the push services cap it anyway
MAX_TAG = 64


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


def drop_held(s: Session, url: str, why: str) -> int:
    """A held push whose subject is gone (a review that was missed) never goes out. The caller commits."""
    rows = s.exec(select(PushLog).where(PushLog.status == "held").where(PushLog.url == url)).all()
    for r in rows:
        r.status, r.why = "dropped", why
        s.add(r)
    return len(rows)


class Pusher:
    def __init__(self, *, engine, secrets_dir: Path, profile: Profile, sender: Callable | None = None,
                 max_per_day: Callable[[], int] | None = None):
        self.engine = engine
        self.profile = profile
        self.max_per_day = max_per_day or (lambda: DEFAULT_MAX)
        self._lock = threading.Lock()
        self.pem, self.public_key = ensure_vapid(Path(secrets_dir))
        if sender is None:
            from pywebpush import webpush as sender
        self._send = sender

    def subscribe(self, endpoint: str, keys: dict, device: str | None) -> None:
        if not {"p256dh", "auth"} <= set(keys):
            raise ValueError("not a push subscription")
        if not outbound.push_endpoint_ok(endpoint):
            raise ValueError("not a browser push service")
        with Session(self.engine) as s:
            row = s.exec(select(PushSubscription).where(PushSubscription.endpoint == endpoint)).first()
            row = row or PushSubscription(endpoint=endpoint, keys_json="{}")
            row.keys_json = json.dumps({"p256dh": keys["p256dh"], "auth": keys["auth"]})
            row.device = (device or "")[:100] or None
            s.add(row)
            s.commit()

    def notify(self, title: str, body: str, url: str = "/", *, source: str = "kairo", ignore_quiet: bool = False,
               wait: bool = False, now: datetime | None = None, ttl: int | None = None, urgency: str | None = None,
               tag: str | None = None, report: dict | None = None) -> str:
        """Sent, held (quiet hours) or dropped (duplicate, budget, or into the brief). Returns which.

        ttl, urgency, tag (G10, timers): how long the push service keeps the push for a phone it cannot reach
        (pywebpush sends 0 = never), RFC 8030's Urgency ("high" wakes the phone), and a notification tag that is
        its own: the phone replaces a notification with the same tag, so two timers need two tags. A tag is part
        of what makes a push "the same push" for the 6 h dedupe. Without them the push is exactly as before.

        report (G10, timers): a dict that is filled with {"tried": devices tried, "delivered": devices whose push service took
        it, "error": the first failure or None}, once the sending is over (so asking for one waits for it, up to 30 s). A push
        that no device took is marked "failed" in push_log (with the reason), which also lets the caller try it again: a
        "sent" row would make the retry "the same push, less than 6 h ago". Without a report nothing changes: the row stays
        "sent" whatever happens to the sending, as it always was."""
        now = (now or datetime.now(self.profile.tz)).astimezone(self.profile.tz)
        body = (body or "")[:1000]
        tag = (tag or "")[:MAX_TAG] or None
        key = hashlib.sha1(f"{source}|{title}|{body}{'|' + tag if tag else ''}".encode("utf-8")).hexdigest()
        with self._lock, Session(self.engine) as s:
            status, why, after = "sent", "", None
            recent = s.exec(select(PushLog).where(PushLog.key == key).where(PushLog.status.in_(("sent", "held")))
                            .where(PushLog.ts >= (now - DEDUPE).astimezone(timezone.utc))).first()
            if recent is not None:
                status, why = "dropped", "the same push was sent less than 6 h ago"
            elif not ignore_quiet and in_quiet_hours(self.profile, now):
                if source in TO_BRIEF:
                    status, why = "dropped", "quiet hours: shown at the top of the morning brief"
                else:
                    status, why, after = "held", "quiet hours", self._quiet_end(now)
            elif source in PROACTIVE and self.proactive_today(s, now) >= self.max_per_day():
                status, why = "dropped", f"daily budget of {self.max_per_day()} proactive pings used"
            row = PushLog(ts=now.astimezone(timezone.utc), source=source, title=title[:200], body=body, url=url, key=key,
                          status=status, why=why, send_after=after.astimezone(timezone.utc) if after else None)
            s.add(row)
            s.commit()
            log_id = row.id
        if status != "sent":
            log.info("push %s (%s): %r", status, why, title)
            return status
        worker = threading.Thread(target=self._send_all, args=(title, body, url),
                                  kwargs={"ttl": ttl, "urgency": urgency, "tag": tag, "report": report,
                                          "log_id": log_id if report is not None else None}, daemon=True)
        worker.start()
        if wait or report is not None:
            worker.join(timeout=30)
        return status

    def proactive_today(self, s: Session | None = None, now: datetime | None = None) -> int:
        now = (now or datetime.now(self.profile.tz)).astimezone(self.profile.tz)
        start = datetime.combine(now.date(), dtime.min, tzinfo=self.profile.tz).astimezone(timezone.utc)
        own = s is None
        s = s or Session(self.engine)
        try:
            return len(s.exec(select(PushLog.id).where(PushLog.status == "sent").where(PushLog.source.in_(sorted(PROACTIVE)))
                              .where(PushLog.ts >= start)).all())
        finally:
            if own:
                s.close()

    def _quiet_end(self, now: datetime) -> datetime:
        end = dtime.fromisoformat(self.profile.quiet_hours.end)
        at = datetime.combine(now.date(), end, tzinfo=self.profile.tz)
        return at if at > now else at + timedelta(days=1)

    def flush(self, now: datetime | None = None, wait: bool = False) -> int:
        """After the quiet hours: what was held goes out, as one push when there is more than one."""
        now = (now or datetime.now(self.profile.tz)).astimezone(self.profile.tz)
        if in_quiet_hours(self.profile, now):
            return 0
        with self._lock, Session(self.engine) as s:
            rows = s.exec(select(PushLog).where(PushLog.status == "held")
                          .where(PushLog.send_after <= now.astimezone(timezone.utc)).order_by(PushLog.id)).all()
            if not rows:
                return 0
            if len(rows) == 1:
                title, body, url = rows[0].title, rows[0].body, rows[0].url
                why = "after the quiet hours"
            else:
                title = f"While you slept: {len(rows)} things"
                body = " · ".join(f"{r.body}" if r.title in ("Kairo", "") else f"{r.title}: {r.body}" for r in rows)[:1000]
                url, why = "/#today", "after the quiet hours (merged)"
            for r in rows:
                r.status, r.why = "sent", why
                s.add(r)
            s.commit()
        worker = threading.Thread(target=self._send_all, args=(title, body, url), daemon=True)
        worker.start()
        if wait:
            worker.join(timeout=30)
        return len(rows)

    def _send_all(self, title: str, body: str, url: str, *, ttl: int | None = None, urgency: str | None = None,
                  tag: str | None = None, report: dict | None = None, log_id: int | None = None) -> None:
        data = {"title": title, "body": body[:240], "url": url}
        if tag:
            data["tag"] = tag
        payload = json.dumps(data)
        extra: dict = {}                                       # only what was asked for: other pushes stay as they were
        if isinstance(ttl, int) and not isinstance(ttl, bool) and ttl > 0:
            extra["ttl"] = min(ttl, MAX_TTL_S)
        if urgency in URGENCIES:
            extra["headers"] = {"Urgency": urgency}
        claims = {"sub": f"mailto:{self.profile.gmail or 'kairo@localhost'}"}
        tried, delivered, error = 0, 0, None
        with Session(self.engine) as s:
            for sub in s.exec(select(PushSubscription)).all():
                if not outbound.push_endpoint_ok(sub.endpoint):
                    log.warning("push skipped: subscription %s is not on a browser push service", sub.id)
                    continue
                tried += 1
                info = {"endpoint": sub.endpoint, "keys": json.loads(sub.keys_json)}
                try:
                    self._send(subscription_info=info, data=payload, vapid_private_key=str(self.pem),
                               vapid_claims=dict(claims), timeout=10, **extra)
                    delivered += 1
                except Exception as exc:  # pywebpush raises WebPushException with .response
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status in (404, 410):
                        s.delete(sub)   # the device unsubscribed
                        s.commit()
                    error = error or f"{status or type(exc).__name__}: {exc}"[:200]
                    log.warning("push failed (%s): %s", status, exc)
        if report is None:
            return
        if tried == 0:
            error = "no device is subscribed to notifications"
        report.update(tried=tried, delivered=delivered, error=None if delivered else error)
        if delivered == 0 and log_id is not None:
            with Session(self.engine) as s:                  # the log says what happened, and a retry is not "the same push"
                row = s.get(PushLog, log_id)
                if row is not None and row.status == "sent":
                    row.status, row.why = "failed", (error or "")[:500]
                    s.add(row)
                    s.commit()
