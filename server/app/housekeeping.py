"""Small background duties, once a minute (Milestone C brings the job scheduler).

- After an automatic update (installer/Kairo-AutoUpdate.ps1 writes
  data/updates/last-update.json) and when an update brought new actions
  (data/updates/config-update.json): one push to the owner's devices.
- Google "Testing" mode ends sign-ins after 7 days: push a reminder a day
  before, and once if Google has signed Kairo out. Tokens that outlive 7 days
  belong to a published app; they get no more reminders.

No pushes in the quiet hours: notices wait until the morning.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

from .google.client import GoogleAPI, GoogleError, GoogleNotConnected, load_token, save_token
from .push import in_quiet_hours

log = logging.getLogger("kairo.housekeeping")

DAY = 86400
TESTING_DAYS = 7
UNKNOWN_AGE_CHECK_S = 6 * 3600   # tokens from before issue times were kept


class Housekeeping:
    def __init__(self, app) -> None:
        self.app = app
        self.settings = app.state.settings
        self._last_unknown_check: dict[str, float] = {}
        self._signed_out_pushed = False

    @property
    def secrets(self) -> Path:
        return self.settings.secrets_dir

    def _quiet(self) -> bool:
        return in_quiet_hours(self.app.state.pusher.profile)

    def _push(self, body: str, url: str = "/#more") -> None:
        self.app.state.pusher.notify("Kairo", body, url, source="system")

    def run_once(self) -> None:
        for duty in (self.update_notice, self.google_reminder, self.held_pushes):
            try:
                duty()
            except Exception:
                log.exception("housekeeping: %s failed", duty.__name__)

    def held_pushes(self) -> None:
        """Fix plan item 20: what the quiet hours held goes out when they end (merged into one push)."""
        flush = getattr(self.app.state.pusher, "flush", None)
        if flush is not None:
            flush()

    # ---------------------------------------------------------------- updates

    def update_notice(self) -> None:
        """One push for the update note and the config note (new actions, fix P0.4) together."""
        if self._quiet():
            return
        pending = []
        for name in ("last-update.json", "config-update.json"):
            path = self.settings.data_dir / "updates" / name
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                if not data.get("notified"):
                    pending.append((path, data))
        if not pending:
            return
        self._push(" ".join(str(d.get("message") or "Kairo was updated.") for _, d in pending)[:200])
        for path, data in pending:
            data["notified"] = True
            path.write_text(json.dumps(data), encoding="utf-8")

    # ---------------------------------------------------------------- Google

    def _refresh_works(self, kind: str) -> bool:
        api = GoogleAPI(self.secrets, kind, transport=getattr(self.app.state, "google_transport", None))
        try:
            api.refresh()
            return True
        except GoogleNotConnected:
            return False
        except (GoogleError, httpx.HTTPError, OSError) as exc:   # Google or the network is down: not a sign-out
            log.warning("google %s check failed: %s", kind, exc)
            return True
        finally:
            api.close()

    def google_reminder(self) -> None:
        if self._quiet():
            return
        now = time.time()
        reminded = False
        self._signed_out_pushed = False
        for kind in ("read", "write"):
            tok = load_token(self.secrets, kind)
            if not tok or not tok.get("refresh_token") or tok.get("no_expiry") or tok.get("signed_out_notice"):
                continue
            issued = tok.get("issued_at")
            if issued is None:
                if now - self._last_unknown_check.get(kind, 0) < UNKNOWN_AGE_CHECK_S:
                    continue
                self._last_unknown_check[kind] = now
                if not self._refresh_works(kind):
                    self._signed_out(kind)
                continue
            age = now - issued
            if age >= TESTING_DAYS * DAY + 3600:
                if self._refresh_works(kind):
                    self._update(kind, no_expiry=True)     # published app: tokens keep working
                else:
                    self._signed_out(kind)
            elif (TESTING_DAYS - 1) * DAY <= age < TESTING_DAYS * DAY and not tok.get("reminded"):
                if not reminded:
                    self._push("Google sign-in for Kairo may end tomorrow (Google Testing mode). "
                               "Tap, then More -> Google -> Sign in again.")
                    reminded = True
                self._update(kind, reminded=True)

    def _signed_out(self, kind: str) -> None:
        if not self._signed_out_pushed:
            self._push("Google signed Kairo out, so mail and calendar are paused. "
                       "Tap, then More -> Google -> Sign in again.")
            self._signed_out_pushed = True
        self._update(kind, signed_out_notice=True)

    def _update(self, kind: str, **fields) -> None:
        tok = load_token(self.secrets, kind) or {}
        tok.update(fields)
        save_token(self.secrets, kind, tok)


async def run_forever(hk: Housekeeping, *, first_delay: float = 30.0, every: float = 60.0) -> None:
    await asyncio.sleep(first_delay)
    while True:
        await asyncio.to_thread(hk.run_once)
        await asyncio.sleep(every)
