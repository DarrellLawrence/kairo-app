"""The Today card (owner's request 2026-10-16): what the voice model answers from at once.

Most questions on a call are predictable: what's next, what's left today, the weather, the next train.
The card holds those, always fresh, so the voice model on the PC answers them straight away; only what
the card can't answer goes to Claude (ask_kairo).

Sections: right now (the day, where the owner is), the next 3 events, today's tasks, the top open
loops, the weather at home, the next connection for the owner's routine (home station -> where they're
heading, from the saved places), key profile facts and what Kairo remembers lately. Event titles,
tasks and loops can come from other people (mail, invites): they are wrapped as data.

The Keeper keeps every section fresh on its own clock (Kairo's own data every minute, the calendar every
3 minutes, the connection every 3 minutes on a call and 10 otherwise, the weather every 30 minutes) and
refreshes a section at once when told it changed (after Claude did something on the call). It never
makes a turn wait: a turn reads the last card; the refresh runs beside it. A call gets only the
sections that changed, at the end of the conversation, so the voice model's prompt cache holds.
Only station names leave the PC (lookup.py: weather and timetable).
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from typing import Callable

from . import dues
from .tools import untrusted

log = logging.getLogger("kairo.todaycard")

SECTIONS = {   # key -> title, in card order
    "now": "Right now",
    "events": "Next events (only the next 3: anything later, ask Kairo)",
    "tasks": "Today's tasks (due today or overdue)",
    "loops": "Open loops (the top ones)",
    "weather": "Weather",
    "trip": "Next connection",
    "about": "About the owner",
    "lately": "What Kairo remembers lately",
}
DATA = {"events": "the calendar", "tasks": "tasks", "loops": "open loops"}   # other people's words: data
LIMITS = {"now": 160, "events": 700, "tasks": 600, "loops": 600, "weather": 200, "trip": 360, "about": 1000, "lately": 500}
LOCAL = ("now", "tasks", "loops")    # Kairo's own data: cheap to read again
# OpenAI Realtime gets only what the owner was told it gets (2026-10-15): no tasks, weather or routine.
REALTIME = ("now", "events", "loops", "about", "lately")
TTL_S = {"now": 60, "tasks": 60, "loops": 60, "events": 180, "trip": 600, "weather": 1800, "about": 600, "lately": 600}
TRIP_ON_CALL_S = 180
NOTHING = "(none)"


# ---------------------------------------------------------------- the lines (pure, tested)

def _local(t: dt.datetime | str | None, tz) -> dt.datetime | None:
    if t is None:
        return None
    if isinstance(t, str):
        try:
            t = dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
        except ValueError:
            return None
    return (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).astimezone(tz)


def now_line(now: dt.datetime, place: str | None = None) -> str:
    where = f"; the owner is at {place}" if place else ""
    return f"{now:%A %d %B %Y}{where}"


def events_lines(events: list[dict] | None, now: dt.datetime, *, n: int = 3, unreadable: str | None = None) -> str:
    """The next n events from now (today and tomorrow), or why the calendar can't be known."""
    if unreadable:
        return unreadable
    tz, out = now.tzinfo, []
    for e in events or []:
        if e.get("all_day"):
            try:
                day = dt.date.fromisoformat(str(e.get("start"))[:10])
            except ValueError:
                continue
            if day >= now.date():
                out.append(f"- {day:%a} all day: {e.get('summary') or '(no title)'}")
            continue
        st, en = _local(e.get("start"), tz), _local(e.get("end"), tz)
        if st is None or (en or st) < now:
            continue
        when = "now, until " + f"{en:%H:%M}" if st <= now and en else f"{st:%a %H:%M}" + (f"–{en:%H:%M}" if en else "")
        where = f" ({e['location']})" if e.get("location") else ""
        out.append(f"- {when} {e.get('summary') or '(no title)'}{where}")
        if len(out) >= n:
            break
    return "\n".join(out[:n]) or "(nothing more today or tomorrow: the calendar was read)"


DATE_ONLY = ((0, 0), (12, 0), (23, 59))   # a due without a time of day (a date alone is stored as noon: tools._due)


def tasks_lines(tasks: list, now: dt.datetime, *, n: int = 5) -> str:
    """Open tasks due today or overdue, soonest first."""
    end = dt.datetime.combine(now.date(), dt.time.max, tzinfo=now.tzinfo)
    rows = []
    for t in tasks or []:
        due = _local(getattr(t, "due", None), now.tzinfo)
        if due is None or due > end:
            continue
        if dues.overdue(due, now, now.tzinfo):
            when = f" ({dues.words(due, now, now.tzinfo)})"
        else:
            when = "" if (due.hour, due.minute) in DATE_ONLY else f" by {due:%H:%M}"
        rows.append((due, f"- {getattr(t, 'title', '')[:120]}{when}"))
    rows.sort(key=lambda r: r[0])
    return "\n".join(line for _, line in rows[:n]) or "(nothing due today)"


def loops_lines(loops: list, tz, *, n: int = 3, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(tz)
    out = []
    for lp in (loops or [])[:n]:
        who = f" ({lp.who})" if getattr(lp, "who", None) else ""
        due = getattr(lp, "due", None)
        when = f", {dues.words(due, now, tz)}" if due else ""
        urgent = " [urgent]" if getattr(lp, "urgency", None) == "urgent" else ""
        out.append(f"- {lp.kind}: {lp.what[:140]}{who}{when}{urgent}")
    return "\n".join(out) or "(no open loops)"


def weather_line(w: dict | None, where: str | None) -> str:
    if not w:
        return "(not known: no home station saved, or the weather service didn't answer)"
    at = f"{where}: " if where else ""
    rain = f", rain {w['rain_pct']}%" if w.get("rain_pct") is not None else ""
    return (f"{at}now {_deg(w.get('now_c'))}, {w.get('now') or '–'}; today {_deg(w.get('min_c'))} to "
            f"{_deg(w.get('max_c'))}, {w.get('today') or '–'}{rain}")


def _deg(v) -> str:
    return f"{round(v)}°C" if isinstance(v, (int, float)) else "–"


def trip_lines(conns: list[dict] | None, frm: str | None, to: str | None, tz) -> str:
    if not frm or not to:
        return "(not known: save a home station and where you go, e.g. \"my school station is ...\")"
    if not conns:
        return f"{frm} to {to}: (the timetable didn't answer)"
    out = [f"{frm} to {to}:"]
    for c in conns[:3]:
        dep, arr = _local(c.get("departure"), tz), _local(c.get("arrival"), tz)
        if dep is None:
            continue
        bits = [f"{dep:%H:%M}"]
        if c.get("line"):
            bits.append(c["line"])
        if c.get("platform"):
            bits.append(f"platform {c['platform']}")
        if arr is not None:
            bits.append(f"arrives {arr:%H:%M}")
        if c.get("cancelled"):
            bits.append("CANCELLED")
        elif c.get("delay"):
            bits.append(f"{c['delay']} min late")
        out.append("- " + ", ".join(bits))
    return "\n".join(out)


def render(sections: dict[str, str], only=None) -> str:
    """The card (or only some sections of it) as the voice model reads it."""
    parts = []
    for key, title in SECTIONS.items():
        if only is not None and key not in only:
            continue
        body = (sections.get(key) or "").strip()[:LIMITS[key]]
        if not body:
            if only is None:
                continue                                   # nothing known: not even a header
            body = NOTHING
        if key in DATA and body != NOTHING:
            body = untrusted(DATA[key], body)
        parts.append(f"## {title}\n{body}")
    return "\n\n".join(parts)


def changed(old: dict[str, str] | None, new: dict[str, str]) -> list[str]:
    """The sections that differ (in card order)."""
    old = old or {}
    return [k for k in SECTIONS if (new.get(k) or "") != (old.get(k) or "")]


# ---------------------------------------------------------------- always fresh

class Keeper:
    """Every section on its own clock; never makes a reader wait for the network.

    sources: key -> a function that returns the section's lines, or None for "keep what you have"
    (it may raise: the last good value stays, and a section that never worked says it isn't known).
    ttl: key -> seconds.
    """

    def __init__(self, sources: dict[str, Callable[[], str]], *, ttl: dict[str, float] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.sources, self.ttl, self.clock = dict(sources), {**TTL_S, **(ttl or {})}, clock
        self.sections: dict[str, str] = {}
        self.at: dict[str, float] = {}
        self.version = 0
        self._lock = threading.Lock()
        self._busy = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.on_call = False

    def _due(self, key: str, now: float) -> bool:
        ttl = TRIP_ON_CALL_S if key == "trip" and self.on_call else self.ttl.get(key, 300)
        return key not in self.at or now - self.at[key] >= ttl

    def refresh(self, *, force: bool = False, only=None) -> dict[str, str]:
        """Reads every section that is due (all with force). One refresh at a time; returns the card."""
        with self._busy:
            now = self.clock()
            for key, fn in self.sources.items():
                if only is not None and key not in only:
                    continue
                if not force and not self._due(key, now):
                    continue
                try:
                    got = fn()
                    if got is None:            # nothing new to read now (e.g. the timetable at night): keep it
                        with self._lock:
                            self.at[key] = now
                        continue
                    value = str(got).strip()
                except Exception as exc:   # one section failing never empties the card
                    log.warning("today card: %s: %s: %s", key, type(exc).__name__, exc)
                    with self._lock:
                        if key not in self.sections:
                            self.sections[key] = f"(not known right now: {type(exc).__name__})"
                        self.at[key] = now
                    continue
                with self._lock:
                    if self.sections.get(key) != value:
                        self.sections[key] = value
                        self.version += 1
                    self.at[key] = now
        return self.peek()

    def peek(self) -> dict[str, str]:
        with self._lock:
            return dict(self.sections)

    def ready(self) -> bool:
        with self._lock:
            return bool(self.sections)

    def invalidate(self, *keys: str) -> None:
        """Something changed (Claude did something on the call): read these again on the next refresh."""
        with self._lock:
            for k in keys or tuple(self.sources):
                self.at.pop(k, None)

    def refresh_soon(self, *keys: str) -> None:
        """Invalidate and refresh beside the caller (never blocks a turn)."""
        self.invalidate(*keys)
        threading.Thread(target=self._safe_refresh, name="today-card", daemon=True).start()

    def warm(self) -> None:
        """Read what's due beside the caller (opening the Call screen, a turn): never blocks."""
        if not self._busy.locked():
            threading.Thread(target=self._safe_refresh, name="today-card", daemon=True).start()

    def _safe_refresh(self) -> None:
        try:
            self.refresh()
        except Exception:
            log.exception("today card")

    def start(self, every_s: float = 30.0) -> None:
        if self._thread is not None:
            return

        def loop():
            while not self._stop.is_set():
                self._safe_refresh()
                self._stop.wait(every_s)
        self._thread = threading.Thread(target=loop, name="today-card", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
