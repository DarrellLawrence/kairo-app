"""Timers and short reminders the owner sets by voice (fast paths, G10; the owner's request in the Phase 1 voice plan).

"Set a timer for ten minutes" is answered by a rule on the PC (fastpath.py), not by a model; this module is what
makes it ring. Their own runner, not the job scheduler: reloading the scheduler's jobs dropped a one-off job that
was still waiting (measured with APScheduler 3.11.3), and a timer must survive that and a restart. The database row
(db.Timer) is the truth; the runner asks it what is due, sleeps until the next one (never longer than CHUNK_S, so a
timer written by another path or a clock that was set is seen within half a minute), and a timer added while it
sleeps wakes it.

When a timer is up, a push goes to the owner's devices through the one notification boss (push.Pusher.notify):
- quiet hours do not hold it back: the owner asked for exactly this moment;
- it has a time-to-live, so the push service keeps it for a phone it cannot reach right now (the push library's own 0 means
  "never"), and urgency high, so the phone wakes for it (RFC 8030);
- its notification tag is its own (timer-<id>), so two timers that ring together are two notifications and the 6 h
  "same push" rule cannot swallow the second;
- the text is a fixed template plus the owner's own words (the label, cleaned): never text a model wrote. The set
  time is in it, which also keeps two identical timers apart in the log.

This is an internal action that only touches the owner (v7): no approval card, no gate action, no tool for the model.
A timer that ran out while Kairo was not running still rings after the restart (told that it was late); one that
is over STALE_S late is marked missed and not told, since it would only be noise by now.

A timer is only "fired" when a device's push service took the push (notify's report). If none did (a timeout, a
push service that said no, no device subscribed) the timer stays waiting with the reason in its note, is tried
again a little later each time (the push is marked "failed" in push_log, so the retry is allowed) and is missed, with
the reason, once it is STALE_S late. Setting a timer is refused in plain words when no device is subscribed at all
(can_ring): Kairo does not promise a ring nobody would hear.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from typing import Callable

from sqlmodel import select

from .db import PushSubscription, Timer, session_for

log = logging.getLogger("kairo.timers")

CHUNK_S = 30.0          # the longest sleep of the runner
MIN_S, MAX_S = 1, 24 * 3600
MAX_ACTIVE = 20
LABEL_MAX = 60
LATE_NOTE_S = 120       # later than this when it rings: the push says so
STALE_S = 30 * 60       # later than this: missed, not told
PUSH_TTL_S = 600        # the push service keeps an undelivered timer push for ten minutes
KEEP_DAYS = 30          # a finished timer is forgotten after this
PURGE_EVERY_H = 6
RETRY_S = 5.0           # a timer that is due but could not be told is tried again after this, and later each time (half
                        # as long as it is late, at most one chunk)
TRYING = "push failed: "    # a waiting timer's note after a try that no device took
UTC = dt.timezone.utc


class TimerError(ValueError):
    """What the owner asked for cannot be set. kind: "range" (a second to a day) or "many" (too many at once)."""

    def __init__(self, message: str, kind: str = "range"):
        super().__init__(message)
        self.kind = kind


def _utc(t: dt.datetime) -> dt.datetime:
    """SQLite gives datetimes back without their zone: they were stored as UTC."""
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).astimezone(UTC)


# ---------------------------------------------------------------- words (pure)

_CTRL = re.compile(r"[\x00-\x1f\x7f<>\[\]{}|`\\]+")
_EDGE = re.compile(r"^[\s,.;:!?'\"-]+|[\s,;:!?'\"-]+$")


def clean_label(text: str | None) -> str | None:
    """The owner's words for a label: one line, no markup or brackets, at most LABEL_MAX characters (cut at a word)."""
    t = re.sub(r"\s+", " ", _CTRL.sub(" ", text or "")).strip()
    t = _EDGE.sub("", t)
    if len(t) > LABEL_MAX:
        t = t[:LABEL_MAX].rsplit(" ", 1)[0] if " " in t[:LABEL_MAX] else t[:LABEL_MAX]
        t = _EDGE.sub("", t)
    return t or None


_UNITS = {"en": (("hour", "hours", 3600), ("minute", "minutes", 60), ("second", "seconds", 1)),
          "de": (("Stunde", "Stunden", 3600), ("Minute", "Minuten", 60), ("Sekunde", "Sekunden", 1))}


def say_duration(seconds: int, lang: str = "en") -> str:
    """600 -> "10 minutes", 5400 -> "1 hour 30 minutes" (German: "10 Minuten", "1 Stunde 30 Minuten")."""
    left, parts = max(int(seconds), 0), []
    for one, many, size in _UNITS["de" if lang == "de" else "en"]:
        n, left = divmod(left, size)
        if n:
            parts.append(f"{n} {one if n == 1 else many}")
    return " ".join(parts) or ("0 Sekunden" if lang == "de" else "0 seconds")


def say_left(seconds: float, lang: str = "en") -> str:
    """What is left, as people say it: under a minute in seconds (to 5), else whole minutes (hours and minutes)."""
    s = max(int(round(seconds)), 0)
    if s < 60:
        return say_duration(max(5 * round(s / 5), 5) if s >= 3 else max(s, 1), lang)
    return say_duration(60 * round(s / 60), lang)


def local_hhmm(t: dt.datetime, tz) -> str:
    return _utc(t).astimezone(tz).strftime("%H:%M")


def words(t: Timer, tz, late_s: float = 0.0) -> tuple[str, str]:
    """(title, body) of the push: a fixed template and the owner's own words, in the language it was set in. A timer whose
    earlier tries failed (note starts with TRYING) says that, not that Kairo was not running."""
    de = t.lang == "de"
    set_at = local_hhmm(t.created_at, tz)
    ran_out = local_hhmm(t.fire_at, tz)
    if t.kind == "reminder":
        title = "Erinnerung" if de else "Reminder"
        if t.label:
            body = f"{title}: {t.label}"
        else:
            body = "Du wolltest erinnert werden" if de else "You asked me to remind you"
        body += f" (gestellt um {set_at})." if de else f" (set {set_at})."
    else:
        title = "Timer"
        dur = say_duration(t.seconds, t.lang)
        body = (f"Dein Timer für {dur} ist abgelaufen" if de else f"Your timer for {dur} is up")
        body += (f": {t.label}" if t.label else "")
        body += f" (gestellt um {set_at})." if de else f" (set {set_at})."
    if late_s >= LATE_NOTE_S:
        retried = (t.note or "").startswith(TRYING)
        if retried:
            body += (f" Er ist um {ran_out} abgelaufen; die ersten Versuche, dich zu erreichen, sind nicht durchgekommen." if de
                     else f" It ran out at {ran_out}; the first tries to tell you did not get through.")
        else:
            body += (f" Er ist um {ran_out} abgelaufen, als Kairo nicht lief." if de
                     else f" It ran out at {ran_out}, while Kairo was not running.")
    return title, body


# ---------------------------------------------------------------- the runner

class Timers:
    """notify: push.Pusher.notify (or anything with its signature); now: the clock (an aware datetime), injected in tests."""

    def __init__(self, engine, notify: Callable[..., str], *, tz, now: Callable[[], dt.datetime] | None = None,
                 chunk_s: float = CHUNK_S):
        self.engine, self.notify, self.tz, self.chunk_s = engine, notify, tz, chunk_s
        self.now = now or (lambda: dt.datetime.now(UTC))
        self._purged_at: dt.datetime | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None

    # ------------------------------------------------ what the owner does

    def add(self, seconds: int, *, label: str | None = None, kind: str = "timer", lang: str = "en") -> Timer:
        if not isinstance(seconds, int) or isinstance(seconds, bool) or not MIN_S <= seconds <= MAX_S:
            raise TimerError("a timer can be from a second to a day", "range")
        now = _utc(self.now())
        with session_for(self.engine) as s:
            if len(s.exec(select(Timer.id).where(Timer.status == "active")).all()) >= MAX_ACTIVE:
                raise TimerError(f"there are already {MAX_ACTIVE} timers", "many")
            row = Timer(created_at=now, fire_at=now + dt.timedelta(seconds=seconds), seconds=seconds,
                        label=clean_label(label), kind="reminder" if kind == "reminder" else "timer",
                        lang="de" if lang == "de" else "en")
            s.add(row)
            s.commit()
        self._poke()
        return row

    def can_ring(self) -> bool:
        """At least one device is subscribed to notifications: a timer set now has someone to ring."""
        with session_for(self.engine) as s:
            return s.exec(select(PushSubscription.id).limit(1)).first() is not None

    def active(self) -> list[Timer]:
        with session_for(self.engine) as s:
            return list(s.exec(select(Timer).where(Timer.status == "active").order_by(Timer.fire_at, Timer.id)).all())

    def left(self, t: Timer) -> float:
        return (_utc(t.fire_at) - _utc(self.now())).total_seconds()

    def cancel(self, ids: list[int]) -> int:
        now, n = _utc(self.now()), 0
        with session_for(self.engine) as s:
            for i in ids:
                row = s.get(Timer, i)
                if row is not None and row.status == "active":
                    row.status, row.done_at, row.note = "cancelled", now, "cancelled by the owner"
                    s.add(row)
                    n += 1
            s.commit()
        self._poke()
        return n

    # ------------------------------------------------ ringing

    def fire_due(self, now: dt.datetime | None = None) -> int:
        """Rings (or, when far too late, marks missed) everything whose time has come. Returns how many rang. A timer no
        device could be told about stays waiting (note: why) and is tried again at the next tick."""
        now = _utc(now or self.now())
        rang = 0
        can_ring = self.can_ring()
        with session_for(self.engine) as s:
            due = s.exec(select(Timer).where(Timer.status == "active").where(Timer.fire_at <= now)
                         .order_by(Timer.fire_at, Timer.id)).all()
            for t in due:
                late = (now - _utc(t.fire_at)).total_seconds()
                if late > STALE_S:
                    reason = (t.note or "")[len(TRYING):].strip() if (t.note or "").startswith(TRYING) else "Kairo was not running"
                    t.status, t.done_at = "missed", now
                    t.note = f"{int(late // 60)} min late ({reason}): not told"
                    log.warning("timer %s: %s", t.id, t.note)
                else:
                    failure, report = None, {}
                    if not can_ring:
                        failure = "no device is subscribed to notifications"     # nothing to try: not a push into the void
                    else:
                        title, body = words(t, self.tz, late)
                        try:
                            self.notify(title, body, "/", source="timer", ignore_quiet=True, ttl=PUSH_TTL_S, urgency="high",
                                        tag=f"timer-{t.id}", report=report)
                        except Exception as exc:   # tried again at the next tick; never lose the runner over it
                            log.exception("timer %s: the push failed", t.id)
                            failure = f"{type(exc).__name__}: {exc}"[:200]
                        else:
                            if report.get("delivered") == 0:                         # asked, answered, and nobody took it
                                failure = report.get("error") or "no device took the push"
                    if failure is not None:
                        t.note = TRYING + failure
                        log.warning("timer %s: %s", t.id, t.note)
                        s.add(t)
                        s.commit()
                        continue
                    t.status, t.done_at = "fired", now
                    note = f"rang {int(late)} s late" if late >= LATE_NOTE_S else None
                    if report.get("delivered") is not None and report.get("tried", 0) > report["delivered"]:
                        note = "; ".join(x for x in (note, f"delivered to {report['delivered']} of {report['tried']} devices") if x)
                    t.note = note
                    rang += 1
                s.add(t)
                s.commit()
        return rang

    def purge(self) -> int:
        """Forgets timers that finished more than KEEP_DAYS ago (fired, cancelled or missed). Waiting ones are never touched."""
        cutoff = _utc(self.now()) - dt.timedelta(days=KEEP_DAYS)
        with session_for(self.engine) as s:
            old = s.exec(select(Timer).where(Timer.status != "active").where(Timer.done_at < cutoff)).all()
            for t in old:
                s.delete(t)
            s.commit()
        return len(old)

    def next_fire(self) -> dt.datetime | None:
        with session_for(self.engine) as s:
            row = s.exec(select(Timer.fire_at).where(Timer.status == "active").order_by(Timer.fire_at).limit(1)).first()
        return _utc(row) if row is not None else None

    def _tick(self) -> float:
        """Rings what is due, cleans up now and then, then says how long to sleep."""
        self.fire_due()
        now = _utc(self.now())
        if self._purged_at is None or now - self._purged_at >= dt.timedelta(hours=PURGE_EVERY_H):
            self._purged_at = now
            self.purge()
        nxt = self.next_fire()
        if nxt is None:
            return self.chunk_s
        gap = (nxt - _utc(self.now())).total_seconds()
        if gap <= 0:                      # due but not rung (the push failed): not in a tight loop, later each time
            return min(max(RETRY_S, -gap / 2), self.chunk_s)
        return min(gap + 0.005, self.chunk_s)

    async def run(self) -> None:
        """Until cancelled. Reads the database at every wake-up, so a restart needs no reload step."""
        self._loop, self._wake = asyncio.get_running_loop(), asyncio.Event()
        try:
            while True:
                self._wake.clear()
                try:
                    wait = await asyncio.to_thread(self._tick)
                except Exception:   # the database was busy or the like: look again soon
                    log.exception("timers: tick")
                    wait = RETRY_S
                try:
                    await asyncio.wait_for(self._wake.wait(), wait)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._loop = self._wake = None

    def _poke(self) -> None:
        """A timer was added or cancelled: the runner looks again now (from any thread)."""
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None:
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:   # the loop is closing
                pass
