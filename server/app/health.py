"""Fix plan item 32: what is wrong with Kairo itself, told once by push and shown as the red chip.

Every 5 minutes (cheap, local): Claude logged out or out of usage, Kairo's tools not connected or the
self-test failed, less than 10 GB free, Syncthing conflict copies in the vault, a crash loop (more than
3 starts in an hour), the auto-update not running for 30 minutes. Every hour (they go over the network): Google with a real API call (read: Gmail
profile; write: a token refresh, like the 7-day check), and Tailscale end to end (this PC calls its own
https://<name>.ts.net/health, the way the iPhone does; any answer means the tailnet path works).

A problem pushes once when it appears (source "health": held in the quiet hours, never against the
ping budget); while it lasts, Today's chip shows it. Network trouble is "unknown", never an alarm.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import shutil
from pathlib import Path

import httpx
from sqlmodel import Session

from . import outbound, vault
from .db import SettingKV
from .google.client import GoogleAPI, GoogleError, GoogleNotConnected, load_token

log = logging.getLogger("kairo.health")

STATE_KEY = "health.state"
DISK_MIN_GB = 10
CRASH_STARTS = 3
CHEAP_EVERY_S = 5 * 60
FULL_EVERY_S = 60 * 60
HOURLY = ("google_read", "google_write", "tailscale")
# Shown on Today from here; Claude, tools and the self-test are already checked live there.
TODAY_KEYS = ("google_read", "google_write", "tailscale", "disk", "conflicts", "crash_loop", "updates")
UPDATER_STALE_MIN = 30   # the auto-update runs every 5 minutes (as SYSTEM) and writes status\updater.json each time
UPDATER_GRACE_MIN = 20   # just after Kairo starts (the PC woke up) the updater may not have run yet


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------- starts (crash loop)

def _starts_path(data_dir: Path) -> Path:
    return Path(data_dir) / "logs" / "starts.json"


PLANNED_S = 15 * 60   # a start this soon after the installer stopped Kairo on purpose is not a crash


def _planned(data_dir: Path, now: dt.datetime) -> bool:
    """The installer noted a stop it made on purpose (update, Kairo Logins: status\\planned-restart.json)."""
    from . import status_files
    note = status_files.read_json(data_dir, "planned-restart.json")
    try:
        at = dt.datetime.fromisoformat(str(note["at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return False
    at = at if at.tzinfo else at.replace(tzinfo=dt.timezone.utc)
    return dt.timedelta(0) <= now - at <= dt.timedelta(seconds=PLANNED_S)


def record_start(data_dir: Path, now: dt.datetime | None = None) -> int:
    """Note this start; returns how many starts there were in the last hour (this one included).
    A start right after a stop the installer made on purpose is not counted."""
    now = now or _now()
    if _planned(data_dir, now):
        return starts_last_hour(data_dir, now)
    path = _starts_path(data_dir)
    try:
        starts = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    except (OSError, ValueError):
        starts = []
    starts = [s for s in starts if isinstance(s, str)][-19:] + [now.isoformat()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(starts), encoding="utf-8")
    return starts_last_hour(data_dir, now)


def starts_last_hour(data_dir: Path, now: dt.datetime | None = None) -> int:
    now = now or _now()
    try:
        starts = json.loads(_starts_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    hour_ago = now - dt.timedelta(hours=1)
    n = 0
    for s in starts:
        try:
            n += dt.datetime.fromisoformat(s) >= hour_ago
        except (TypeError, ValueError):
            pass
    return n


def crash_loop_text(n: int) -> str:
    return f"Kairo restarted {n} times in the last hour: something keeps crashing it. Run diagnosis and send it."


# ---------------------------------------------------------------- the checks

def kairo_url(data_dir: Path) -> str | None:
    """Kairo's tailnet address, recorded by the boot check (boot-check.json, kairo_url), else by the
    auto-update's tailnet snapshot (tailnet.json, every 5 minutes: acceptance #30, the boot check ran days ago)."""
    from . import status_files
    for name in ("boot-check.json", "tailnet.json"):
        data = status_files.read_json(data_dir, name)
        url = data.get("kairo_url") if isinstance(data, dict) else None
        if outbound.tailnet_url_ok(url):     # never anywhere else (a planted file can't redirect it)
            return url
    return None


class HealthWatch:
    def __init__(self, *, settings, engine, runner, pusher, transport=None, http_get=None) -> None:
        """transport: a callable giving the Google HTTP transport (tests swap it in after start)."""
        self.settings, self.engine, self.runner, self.pusher = settings, engine, runner, pusher
        self.transport = transport or (lambda: None)
        self._http_get = http_get or (lambda url: httpx.get(url, timeout=10, follow_redirects=False))
        self._started = _now()
        self._checked: dt.datetime | None = None

    # -- cheap, local

    def cheap(self, now: dt.datetime | None = None) -> dict[str, str]:
        out: dict[str, str] = {}
        brain = self.runner.brain
        status = brain.health().value
        if status == "logged_out":
            out["claude"] = "Claude is logged out, so Kairo can't think. On the PC: Start menu -> Kairo Logins."
        elif status == "usage_limited":
            out["claude"] = "Claude's usage limit is reached. Kairo waits until it resets."
        elif status == "unavailable":
            out["claude"] = "The Claude CLI isn't working on the PC. More -> System shows why."
        tools = getattr(brain, "tools_status", lambda: "unknown")()
        if tools.startswith("failed"):
            out["tools"] = "Kairo's tools are not connected: it can't act until this is fixed."
        with Session(self.engine) as s:
            row = s.get(SettingKV, "selftest.tools")
        try:
            last = json.loads(row.value) if row and row.value else None
        except ValueError:
            last = None
        if last and last.get("ok") is False:
            out["selftest"] = f"Self-test failed: {last.get('reason') or 'see More -> System'}"
        try:
            free_gb = shutil.disk_usage(self.settings.data_dir).free / 1e9
            if free_gb < DISK_MIN_GB:
                out["disk"] = f"Only {free_gb:.1f} GB free on the PC. Below 10 GB backups and updates can fail."
        except OSError:
            pass
        n = len(vault.conflicts(self.settings.vault_dir))
        if n:
            out["conflicts"] = (f"{n} note{'s were' if n > 1 else ' was'} changed on two devices at once. "
                                "Pick a version: More -> System -> Sync conflicts.")
        starts = starts_last_hour(self.settings.data_dir, now)
        if starts > CRASH_STARTS:
            out["crash_loop"] = crash_loop_text(starts)
        now = now or _now()
        if self._checked is not None and (now - self._checked).total_seconds() > 3 * CHEAP_EVERY_S:
            self._started = now              # the PC slept (or Kairo hung): the updater gets its grace again
        self._checked = now
        stale = self._updater_stopped(now)
        if stale:
            out["updates"] = stale
        return out

    def _updater_stopped(self, now: dt.datetime) -> str | None:
        """Owner's rule 2026-10-17: an update that fails is never silent. The auto-update notes its own
        failures for the push; this catches the one it can't: it stopped running (its task is off, or the
        script breaks before it starts)."""
        from . import status_files
        note = status_files.read_json(self.settings.data_dir, "updater.json")
        if not isinstance(note, dict) or not note.get("at"):
            return None                      # an install from before 2026-10-17, or a PC without the updater
        try:
            at = dt.datetime.fromisoformat(str(note["at"]).replace("Z", "+00:00"))
        except ValueError:
            return None
        if at.tzinfo is None:
            at = at.astimezone()             # local time on the PC
        mins = (now - at).total_seconds() / 60
        if mins < UPDATER_STALE_MIN or (now - self._started).total_seconds() / 60 < UPDATER_GRACE_MIN:
            return None
        ago = f"{int(mins // 60)} hours" if mins >= 120 else f"{int(mins)} minutes"
        return (f"The auto-update hasn't run for {ago}, so new versions don't arrive. "
                "On the PC: Start menu -> Kairo -> Update Kairo shows why.")

    # -- hourly, over the network ("unknown" leaves the last result as it was)

    def hourly(self, before: dict[str, str]) -> dict[str, str]:
        out = {k: v for k, v in before.items() if k in HOURLY}
        for kind in ("read", "write"):
            key = f"google_{kind}"
            ok = self._google_ok(kind)
            if ok is True:
                out.pop(key, None)
            elif ok is False:
                out[key] = (f"Google signed Kairo out ({'reading' if kind == 'read' else 'sending'} mail and calendar). "
                            "Sign in again: More -> Google.")
        url = kairo_url(self.settings.data_dir)
        if url:
            try:
                self._http_get(url.rstrip("/") + "/health")
                out.pop("tailscale", None)
            except (httpx.HTTPError, OSError) as exc:
                out["tailscale"] = (f"The iPhone can't reach Kairo: {url} doesn't answer ({type(exc).__name__}). "
                                    "Is Tailscale running and signed in on the PC?")
        return out

    def _google_ok(self, kind: str) -> bool | None:
        tok = load_token(self.settings.secrets_dir, kind)
        if not tok or not tok.get("refresh_token"):
            return None                        # not set up yet: the setup steps say so
        api = GoogleAPI(self.settings.secrets_dir, kind, transport=self.transport())
        try:
            if kind == "read":
                api.get("/gmail/v1/users/me/profile")   # a real call, not "the file exists"
            else:
                api.refresh()                            # the write token is only ever used to send by executor.py
            return True
        except GoogleNotConnected:
            return False
        except GoogleError as exc:
            if exc.status in (401, 403):
                return False
            log.warning("google %s check: %s", kind, exc)
            return None
        except (httpx.HTTPError, OSError) as exc:      # Google or the network is down: not a sign-out
            log.warning("google %s check: %s", kind, exc)
            return None
        finally:
            api.close()

    # -- state, pushes

    def load(self) -> dict:
        with Session(self.engine) as s:
            row = s.get(SettingKV, STATE_KEY)
        try:
            data = json.loads(row.value) if row and row.value else {}
        except ValueError:
            data = {}
        data.setdefault("problems", {})
        return data

    def run(self, *, full: bool, now: dt.datetime | None = None) -> dict[str, str]:
        now = now or _now()
        state = self.load()
        before = {k: v["text"] for k, v in state["problems"].items()}
        current = self.cheap(now)
        current.update(self.hourly(before) if full else {k: v for k, v in before.items() if k in HOURLY})
        new = [k for k in current if k not in before]
        state["problems"] = {k: {"text": t, "since": state["problems"].get(k, {}).get("since") or now.isoformat()}
                             for k, t in current.items()}
        state["checked_at"] = now.isoformat()
        if full:
            state["full_at"] = now.isoformat()
        with Session(self.engine) as s:
            row = s.get(SettingKV, STATE_KEY) or SettingKV(key=STATE_KEY)
            row.value = json.dumps(state)
            s.add(row)
            s.commit()
        if new:
            self.pusher.notify("Kairo", " ".join(current[k] for k in new)[:900], "/#more", source="health")
        return current


def today_issues(s: Session) -> list[str]:
    """For Today's chip: the problems found by the watch that Today doesn't check live itself."""
    row = s.get(SettingKV, STATE_KEY)
    try:
        problems = (json.loads(row.value) if row and row.value else {}).get("problems") or {}
    except ValueError:
        return []
    return [problems[k]["text"] for k in TODAY_KEYS if k in problems]
