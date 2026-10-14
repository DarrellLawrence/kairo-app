"""Scheduled jobs (KICKOFF 12): reviews (Milestone M), email triage, morning
brief and capture processing (Milestone C).

APScheduler in the backend process, in the owner's timezone. Rules for every job:
- one at a time (a queue: a job waits for the one before it);
- nothing in the quiet window (schedule.yaml) unless the owner starts it;
- nothing while the kill switch is on;
- jobs that need the model stop for the day at the daily run budget, and
  pause when the model reports a usage limit (banner in the app);
- every run is logged in job_runs; a failed job never crashes Kairo.
A run that was missed (PC off) still happens when Kairo is back, within the
grace time.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from dataclasses import dataclass
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlmodel import Session, select

from . import killswitch, reviews
from .db import JobRun, SettingKV, utcnow

log = logging.getLogger("kairo.scheduler")

DEFAULT_TIMES = {"daily": "21:00", "big": "18:00"}
DEFAULT_QUIET = ("23:00", "06:00")
DEFAULT_BUDGET = 60
GRACE_S = 6 * 3600
QUEUE_WAIT_S = 30 * 60
USAGE_PAUSE = dt.timedelta(hours=1)

DEFAULT_CRON = {
    "email_triage": "0 7-21 * * *",
    "morning_brief.weekdays": "20 6 * * 1-5",
    "morning_brief.weekends": "20 8 * * 6,0",
    "backup": "30 3 * * *",
    "restore_test": "0 4 * * 0",
    "lerndoku_prompt": "30 17 * * 5",
    "promises": "10 7-21 * * *",
}
# Silent housekeeping: no model, no push unless it fails. Allowed in the quiet window.
QUIET_EXEMPT = {"backup", "restore_test"}
CAPTURE_EVERY_MIN = 15
LOOPS_EVERY_MIN = 15

# Shown in the app (More -> Jobs) and runnable on demand.
JOB_TITLES = {
    "email_triage": "Email triage",
    "morning_brief": "Morning brief",
    "capture_process": "Process captures",
    "review.daily": "Evening check-in",
    "review.big": "Sunday review",
    "backup": "Backup",
    "restore_test": "Restore test",
    "lerndoku_prompt": "Lerndokumentation reminder",
    "open_loops": "Open loops",
    "promises": "Find promises in sent mail",
}


class Skipped(Exception):
    """A job decided not to run (nothing to do, Google not connected, ...)."""


def review_times(schedule: dict | None) -> dict[str, str]:
    """`reviews: {daily: "21:00", big: "18:00"}` in schedule.yaml, defaults otherwise."""
    times = dict(DEFAULT_TIMES)
    for k, v in ((schedule or {}).get("reviews") or {}).items():
        if k in times and _is_hhmm(v):
            times[k] = v
    return times


def _is_hhmm(v) -> bool:
    return isinstance(v, str) and len(v) == 5 and v[2] == ":" and v[:2].isdigit() and v[3:].isdigit()


_DOW = ["sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def cron_trigger(expr: str, tz) -> CronTrigger:
    """A standard 5-field cron line. Day-of-week uses cron numbers (0 or 7 =
    Sunday); APScheduler 3 counts from Monday, so numbers become names here."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron needs 5 fields: {expr!r}")
    minute, hour, dom, month, dow = fields

    def name(tok: str) -> str:
        if "-" in tok:
            a, b = tok.split("-", 1)
            return f"{name(a)}-{name(b)}"
        return _DOW[int(tok)] if tok.isdigit() else tok

    if dow != "*":
        dow = ",".join(name(t) for t in dow.split(","))
    return CronTrigger(minute=minute, hour=hour, day=dom, month=month, day_of_week=dow, timezone=tz)


def in_window(now: dt.time, start: str, end: str) -> bool:
    s, e = dt.time.fromisoformat(start), dt.time.fromisoformat(end)
    return (s <= now or now < e) if s > e else (s <= now < e)


@dataclass
class JobContext:
    """Handed to each job body."""

    session: Session
    run: JobRun
    forced: bool          # started by the owner
    jobs: "Jobs"

    def ask(self, prompt: str, system: str, images=None, purpose: str | None = None):
        from .models import JOB_PURPOSE
        return self.jobs.ask_brain(prompt, system, images=images, purpose=purpose or JOB_PURPOSE.get(self.run.job, "summary"))


class Jobs:
    def __init__(self, *, engine, pusher, profile, schedule: dict | None = None,
                 ask: Callable | None = None, bodies: dict[str, Callable] | None = None):
        self.engine = engine
        self.pusher = pusher
        self.profile = profile
        self.schedule = schedule or {}
        self.times = review_times(schedule)
        qw = (self.schedule.get("quiet_window") or {})
        self.quiet = (qw.get("start") if _is_hhmm(qw.get("start")) else DEFAULT_QUIET[0],
                      qw.get("end") if _is_hhmm(qw.get("end")) else DEFAULT_QUIET[1])
        try:
            self.budget = int(((self.schedule.get("brain") or {}).get("max_runs_per_day")) or DEFAULT_BUDGET)
        except (TypeError, ValueError):
            self.budget = DEFAULT_BUDGET
        self._ask = ask                     # (prompt, system) -> AskResult; None = no model
        self.bodies: dict[str, Callable] = dict(bodies or {})   # job name -> fn(JobContext)
        self.scheduler: BackgroundScheduler | None = None
        self._queue = threading.Lock()
        self.bodies.setdefault("review.daily", self._daily_review_body)
        self.bodies.setdefault("review.big", self._big_review_body)

    # ---------------------------------------------------------------- time

    def now(self) -> dt.datetime:
        return dt.datetime.now(self.profile.tz)

    def today(self) -> dt.date:
        return self.now().date()

    def in_quiet(self, now: dt.datetime | None = None) -> bool:
        return in_window((now or self.now()).astimezone(self.profile.tz).time(), *self.quiet)

    # ---------------------------------------------------------------- model use

    def _kv(self, s: Session, key: str) -> str | None:
        row = s.get(SettingKV, key)
        return row.value if row else None

    def _set_kv(self, s: Session, key: str, value: str | None) -> None:
        row = s.get(SettingKV, key) or SettingKV(key=key)
        row.value = value
        s.add(row)
        s.commit()

    def brain_state(self, s: Session) -> dict:
        today = self.today().isoformat()
        used = int(self._kv(s, f"brain_runs:{today}") or 0)
        until = self._kv(s, "brain_paused_until")
        paused = bool(until and dt.datetime.fromisoformat(until) > utcnow())
        return {"runs_today": used, "budget": self.budget, "paused_until": until if paused else None}

    def ask_brain(self, prompt: str, system: str, images=None, purpose: str = "summary"):
        """One model run for a job, counted against the daily budget."""
        if self._ask is None:
            raise Skipped("no model configured")
        with Session(self.engine) as s:
            st = self.brain_state(s)
            if st["paused_until"]:
                raise Skipped(f"model paused (usage limit) until {st['paused_until']}")
            if st["runs_today"] >= self.budget:
                raise Skipped(f"daily model budget of {self.budget} runs reached")
            self._set_kv(s, f"brain_runs:{self.today().isoformat()}", str(st["runs_today"] + 1))
        from .models import pick
        kwargs = {"images": images} if images else {}
        try:
            result = self._ask(prompt, system, purpose=purpose, model=pick(purpose, self.schedule), **kwargs)
        except TypeError:   # simple test doubles take (prompt, system[, images])
            result = self._ask(prompt, system, images) if images else self._ask(prompt, system)
        if result.status == "usage_limited":
            with Session(self.engine) as s:
                self._set_kv(s, "brain_paused_until", (utcnow() + USAGE_PAUSE).isoformat())
            raise Skipped("the model reported a usage limit; jobs pause for an hour")
        return result

    # ---------------------------------------------------------------- running

    def run_job(self, name: str, *, forced: bool = False, **kwargs) -> JobRun:
        """Run one job now (scheduled or on demand). Returns its job_runs row."""
        body = self.bodies.get(name)
        if body is None:
            raise KeyError(name)
        if not self._queue.acquire(timeout=QUEUE_WAIT_S):
            log.warning("job %s gave up waiting for the previous job", name)
            return self._log(name, "skipped", "previous job still running after 30 minutes")
        try:
            with Session(self.engine) as s:
                if killswitch.is_paused(s):
                    return self._log(name, "skipped", "kill switch is on")
                if not forced and name not in QUIET_EXEMPT and self.in_quiet():
                    return self._log(name, "skipped", "quiet window")
                run = JobRun(job=name)
                s.add(run)
                s.commit()
                s.refresh(run)
                try:
                    out = body(JobContext(session=s, run=run, forced=forced, jobs=self), **kwargs)
                    run.status = "ok"
                    if isinstance(out, str):
                        run.output_path = out[:500]
                except Skipped as exc:
                    run.status, run.error = "skipped", str(exc)[:2000]
                except Exception as exc:   # a failed job is logged, never crashes Kairo
                    log.exception("job %s failed", name)
                    s.rollback()
                    run = s.get(JobRun, run.id) or run
                    run.status, run.error = "error", str(exc)[:2000]
                run.finished_at = utcnow()
                s.add(run)
                s.commit()
                s.refresh(run)
                return run
        finally:
            self._queue.release()

    def _log(self, name: str, status: str, error: str) -> JobRun:
        with Session(self.engine) as s:
            run = JobRun(job=name, status=status, error=error, finished_at=utcnow())
            s.add(run)
            s.commit()
            s.refresh(run)
            return run

    def last_runs(self) -> list[dict]:
        out = []
        with Session(self.engine) as s:
            for name, title in JOB_TITLES.items():
                if name not in self.bodies:
                    continue
                r = s.exec(select(JobRun).where(JobRun.job == name).order_by(JobRun.id.desc())).first()
                out.append({"name": name, "title": title,
                            "last": None if r is None else {"status": r.status, "started_at": r.started_at,
                                                            "finished_at": r.finished_at, "error": r.error}})
            out.append({"brain": self.brain_state(s)})
        return out

    # ---------------------------------------------------------------- reviews (Milestone M)

    def _daily_review_body(self, ctx: JobContext, day: dt.date | None = None) -> None:
        day = day or self.today()
        r, created = reviews.create(ctx.session, "daily", day, self.profile.tz)
        if created:
            from .voice import calls
            if calls.options(ctx.session)["checkin"]:
                calls.start(ctx.session, "Evening check-in",
                            "Hi, it's Kairo. Time for your evening check-in. How did today go, and how is your energy, one to five?",
                            self.pusher)
            else:
                self.pusher.notify("Evening check-in", "2 minutes: how did today go?", f"/#reviews/{r.id}")

    def _big_review_body(self, ctx: JobContext, day: dt.date | None = None) -> None:
        day = day or self.today()
        kind = reviews.big_kind_due(day)
        if not kind:
            raise Skipped("no review due today")
        r, created = reviews.create(ctx.session, kind, day, self.profile.tz)
        if created:
            self.pusher.notify(reviews.TITLES[kind], f"About {reviews.MINUTES[kind]} minutes. Kairo filled in what it knows.",
                               f"/#reviews/{r.id}")

    # Old entry points (tests, catch-up); reviews are created even late in the evening.
    def daily_review(self, day: dt.date | None = None) -> None:
        self.run_job("review.daily", forced=True, day=day)

    def big_review(self, day: dt.date | None = None) -> None:
        if reviews.big_kind_due(day or self.today()):
            self.run_job("review.big", forced=True, day=day)

    def catch_up(self, now: dt.datetime | None = None) -> None:
        """Kairo was off at the scheduled time today: create the review now."""
        now = (now or self.now()).astimezone(self.profile.tz)
        if now.strftime("%H:%M") >= self.times["big"]:
            self.big_review(now.date())
        if now.strftime("%H:%M") >= self.times["daily"]:
            self.daily_review(now.date())

    # ---------------------------------------------------------------- schedule

    def _cron(self, key: str) -> str:
        jobs = self.schedule.get("jobs") or {}
        if key.startswith("morning_brief."):
            v = (jobs.get("morning_brief") or {}).get(key.split(".", 1)[1])
        else:
            v = (jobs.get(key) or {}).get("cron")
        return v if isinstance(v, str) and len(v.split()) == 5 else DEFAULT_CRON[key]

    def start(self) -> None:
        tz = self.profile.tz
        sched = BackgroundScheduler(timezone=tz,
                                    job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": GRACE_S})
        dh, dm = self.times["daily"].split(":")
        bh, bm = self.times["big"].split(":")
        sched.add_job(self.daily_review, CronTrigger(hour=int(dh), minute=int(dm), timezone=tz), id="review.daily")
        sched.add_job(self.big_review, CronTrigger(day_of_week="sun", hour=int(bh), minute=int(bm), timezone=tz), id="review.big")
        if "email_triage" in self.bodies:
            sched.add_job(self.run_job, cron_trigger(self._cron("email_triage"), tz), args=["email_triage"], id="email_triage",
                          misfire_grace_time=1800)
        if "morning_brief" in self.bodies:
            for k in ("morning_brief.weekdays", "morning_brief.weekends"):
                sched.add_job(self.run_job, cron_trigger(self._cron(k), tz), args=["morning_brief"], id=k)
        if "open_loops" in self.bodies:
            every = ((self.schedule.get("jobs") or {}).get("open_loops") or {}).get("every_minutes") or LOOPS_EVERY_MIN
            sched.add_job(self.run_job, IntervalTrigger(minutes=int(every), timezone=tz), args=["open_loops"],
                          id="open_loops", misfire_grace_time=600)
        for name in ("backup", "restore_test", "lerndoku_prompt", "promises"):
            if name in self.bodies:
                sched.add_job(self.run_job, cron_trigger(self._cron(name), tz), args=[name], id=name)
        if "capture_process" in self.bodies:
            every = ((self.schedule.get("jobs") or {}).get("capture_process") or {}).get("every_minutes") or CAPTURE_EVERY_MIN
            sched.add_job(self.run_job, IntervalTrigger(minutes=int(every), timezone=tz), args=["capture_process"],
                          id="capture_process", misfire_grace_time=600)
        sched.start()
        self.scheduler = sched
        threading.Thread(target=self.catch_up, name="jobs-catch-up", daemon=True).start()

    def stop(self) -> None:
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
