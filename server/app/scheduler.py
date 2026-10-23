"""Scheduled jobs (KICKOFF 12): reviews (Milestone M), email triage, morning
brief and capture processing (Milestone C).

APScheduler in the backend process, in the owner's timezone. Rules for every job:
- one at a time (a queue: a job waits for the one before it);
- nothing in the quiet hours (profile.yaml / the quiet_hours setting: the one source, also for
  pushes) unless the owner starts it; skipped runs are counted, one summary row per night;
- nothing while the kill switch is on;
- jobs that need the model stop for the day at the daily run budget, and
  pause when the model reports a usage limit (banner in the app);
- every run is logged in job_runs; a failed job never crashes Kairo.
Missed runs (PC off or Kairo restarting): APScheduler keeps no schedule across restarts, so
catch_up() runs what matters when Kairo starts: the brief if it is before 10:00 and today's
brief is missing, triage, open loops and the PA when their last run is older than their interval,
and the reviews (fix plan item 23); reviews that stacked up meanwhile are missed (reviews.tidy).
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlmodel import Session, select

from . import killswitch, reviews
from .brain.local_llm import ANSWER_CHECKS, LocalAnswer
from .chat import AskResult
from .db import JobRun, SettingKV, utcnow
from .router import LOCAL_KIND

log = logging.getLogger("kairo.scheduler")

DEFAULT_TIMES = {"daily": "21:00", "big": "18:00"}
DEFAULT_QUIET = ("23:00", "06:00")
QUIET_SKIPS = "jobs.quiet_skips"           # {"since", "jobs": {name: count}}: the night's skips, until the summary row
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
    "olat": "5 6 * * *",
    "evening_recap": "30 20 * * *",
}
# Silent housekeeping: no model, no push unless it fails. Allowed in the quiet window.
QUIET_EXEMPT = {"backup", "restore_test"}
CAPTURE_EVERY_MIN = 15
LOOPS_EVERY_MIN = 15
PA_EVERY_MIN = 30
COACH_EVERY_MIN = 30

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
    "pa": "PA: birthdays, people, money, tests, patterns",
    "coach": "Coach: training, food and water, study, faith",
    "olat": "School portal (OLAT, read-only)",
    "chat_memory": "Conversation memory",
    "knowledge_call": "Getting-to-know-you call (max 1 a day)",
    "voice_agenda": "Call topics (what Kairo brings up on a call)",
    "voice_lab": "Voice lab (measure the local voice models, pick one)",
    "evening_recap": "Evening recap mail (to your own Gmail)",
    "document": "Document gauntlet (write, render, check, fix)",
}


CRON_JOBS = ("backup", "restore_test", "lerndoku_prompt", "promises", "olat", "evening_recap")
OPT_IN = ("evening_recap",)   # off until the owner sets a time in Settings (prefs.apply writes the cron)


def cron_jobs(schedule: dict | None) -> list[str]:
    """The fixed-time jobs to plan; an opt-in job only once the settings gave it a time."""
    jobs = (schedule or {}).get("jobs") or {}
    return [n for n in CRON_JOBS if n not in OPT_IN or (jobs.get(n) or {}).get("cron")]


class Skipped(Exception):
    """A job decided not to run (nothing to do, Google not connected, ...)."""


def review_times(schedule: dict | None) -> dict[str, str]:
    """`reviews: {daily: "21:00", big: "18:00"}` in schedule.yaml, defaults otherwise."""
    times = dict(DEFAULT_TIMES)
    for k, v in ((schedule or {}).get("reviews") or {}).items():
        if k in times and _is_hhmm(v):
            times[k] = v
    return times


def _aware(t: dt.datetime) -> dt.datetime:
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


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


def _takes_model(ask) -> bool:
    """The real ask takes purpose= and model=. Decided by its signature, never by catching a TypeError: one
    raised inside a real run used to send the job again without a model (the CLI's default, maybe Opus)."""
    try:
        params = inspect.signature(ask).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(p.kind is p.VAR_KEYWORD or p.name == "model" for p in params)


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
                 ask: Callable | None = None, bodies: dict[str, Callable] | None = None,
                 router=None, local=None, call_open: Callable[[], bool] | None = None):
        self.engine = engine
        self.pusher = pusher
        self.profile = profile
        self.schedule = schedule or {}
        self.times = review_times(schedule)
        try:
            self.budget = int(((self.schedule.get("brain") or {}).get("max_runs_per_day")) or DEFAULT_BUDGET)
        except (TypeError, ValueError):
            self.budget = DEFAULT_BUDGET
        self._ask = ask                     # (prompt, system) -> AskResult; None = no model
        self.router = router                # app/router.Router (G11): which route each ask takes; None = Claude, unlogged
        self.local = local                  # app/brain/local_llm.LocalLLM: the local route, used only when the router says so
        self.call_open = call_open or (lambda: False)   # a voice line is open: the local model stays with the voice
        self.bodies: dict[str, Callable] = dict(bodies or {})   # job name -> fn(JobContext)
        self.scheduler: BackgroundScheduler | None = None
        self._queue = threading.Lock()
        self.hold_until = 0.0               # epoch seconds: an update is being installed, no new job starts
        self.bodies.setdefault("review.daily", self._daily_review_body)
        self.bodies.setdefault("review.big", self._big_review_body)

    # ---------------------------------------------------------------- time

    def now(self) -> dt.datetime:
        return dt.datetime.now(self.profile.tz)

    def today(self) -> dt.date:
        return self.now().date()

    @property
    def quiet(self) -> tuple[str, str]:
        """One source of quiet hours (fix plan item 25): the profile, which the setting changes."""
        qh = getattr(self.profile, "quiet_hours", None)
        start, end = getattr(qh, "start", None), getattr(qh, "end", None)
        return (start if _is_hhmm(start) else DEFAULT_QUIET[0], end if _is_hhmm(end) else DEFAULT_QUIET[1])

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

    def _hold(self, st: dict) -> str:
        """Why Claude can't take a job right now (the router keeps a lab-passed job local meanwhile), or ''."""
        if st["paused_until"]:
            return "paused (usage limit)"
        if st["runs_today"] >= self.budget:
            return f"daily budget of {self.budget} runs reached"
        return ""

    def _route_local(self, prompt: str, system: str, decision, model: str):
        """Run a job the router sent to the local model. (the answer, decision) when it is accepted; (None, the
        next decision) when it is refused, and the job goes to Claude."""
        check = ANSWER_CHECKS.get(decision.purpose)
        try:
            if check is None:        # cannot happen (a test keeps ANSWER_CHECKS and LOCAL_ELIGIBLE equal); if it does, Claude takes the job
                got = LocalAnswer(False, "", "no answer check for this purpose", 0)
            else:
                kind = LOCAL_KIND.get(decision.purpose, "small")
                got = self.local.ask(prompt, system, timeout_s=self.router.rules.timeout_s[kind], check=check,
                                     call_open=self.call_open)
        except Exception as exc:
            got = LocalAnswer(False, "", f"{type(exc).__name__} from the local model", 0)
        if got.ok:
            return AskResult(got.text), decision
        with Session(self.engine) as s:
            hold = self._hold(self.brain_state(s))
        return None, self.router.escalate(decision, got.reason, model=model, claude_hold=hold, fault=got.fault)

    def ask_brain(self, prompt: str, system: str, images=None, purpose: str = "summary"):
        """One model run for a job, counted against the daily budget. With the router on (g11.router) the run is logged
        with its route and reason; a lab-passed job may run on the local model and costs no Claude run."""
        if self._ask is None:
            raise Skipped("no model configured")
        from .models import pick
        model = pick(purpose, self.schedule)
        decision = None
        if self.router is not None:
            with Session(self.engine) as s:
                hold = self._hold(self.brain_state(s))
            local_why = self.local.unavailable() if self.local is not None else "there is no local model"
            decision = self.router.route(purpose, prompt, mode="job", model=model, images=bool(images), claude_hold=hold,
                                         call_open=bool(self.call_open()), local_ready=not local_why, local_why=local_why,
                                         local_model=self.local.fingerprint() if self.local is not None else "")
            if decision.route == "local":
                answer, decision = self._route_local(prompt, system, decision, model)
                if answer is not None:
                    return answer
        with Session(self.engine) as s:
            st = self.brain_state(s)
            if st["paused_until"]:
                raise Skipped(f"model paused (usage limit) until {st['paused_until']}")
            if st["runs_today"] >= self.budget:
                raise Skipped(f"daily model budget of {self.budget} runs reached")
            if decision is not None and decision.route == "none":
                raise Skipped(f"no model route is allowed: {decision.reason}")
            self._set_kv(s, f"brain_runs:{self.today().isoformat()}", str(st["runs_today"] + 1))
        kwargs = {"images": images} if images else {}
        if _takes_model(self._ask):
            result = self._ask(prompt, system, purpose=purpose, model=model, **kwargs)
        else:   # simple test doubles take (prompt, system[, images]); a real run never goes without its model
            result = self._ask(prompt, system, images) if images else self._ask(prompt, system)
        if result.status == "usage_limited":
            with Session(self.engine) as s:
                self._set_kv(s, "brain_paused_until", (utcnow() + USAGE_PAUSE).isoformat())
            raise Skipped("the model reported a usage limit; jobs pause for an hour")
        return result

    # ---------------------------------------------------------------- running

    def busy(self) -> bool:
        """A job is running right now (the auto-updater waits for it, fix P2.14)."""
        return self._queue.locked()

    def run_job(self, name: str, *, forced: bool = False, **kwargs) -> JobRun:
        """Run one job now (scheduled or on demand). Returns its job_runs row."""
        body = self.bodies.get(name)
        if body is None:
            raise KeyError(name)
        if not self._queue.acquire(timeout=QUEUE_WAIT_S):
            log.warning("job %s gave up waiting for the previous job", name)
            return self._log(name, "skipped", "previous job still running after 30 minutes")
        try:
            if time.time() < self.hold_until:
                return self._log(name, "skipped", "an update is being installed")
            with Session(self.engine) as s:
                if killswitch.is_paused(s):
                    return self._log(name, "skipped", "kill switch is on")
                if not forced and name not in QUIET_EXEMPT and self.in_quiet():
                    self._count_quiet_skip(s, name)              # no row per skip: one summary a night
                    return JobRun(job=name, status="skipped", error="quiet hours", finished_at=utcnow())
                self._quiet_summary(s)
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

    def _count_quiet_skip(self, s: Session, name: str) -> None:
        data = json.loads(self._kv(s, QUIET_SKIPS) or "{}")
        data.setdefault("since", utcnow().isoformat())
        data.setdefault("jobs", {})[name] = data.get("jobs", {}).get(name, 0) + 1
        self._set_kv(s, QUIET_SKIPS, json.dumps(data))

    def _quiet_summary(self, s: Session) -> None:
        """The first run after the quiet hours writes what was skipped overnight, in one row."""
        raw = self._kv(s, QUIET_SKIPS)
        if not raw:
            return
        data = json.loads(raw)
        counts = ", ".join(f"{k} {v}" for k, v in sorted(data.get("jobs", {}).items()))
        s.add(JobRun(job="quiet_hours", status="skipped", error=f"skipped in the quiet hours: {counts}"[:2000],
                     finished_at=utcnow()))
        self._set_kv(s, QUIET_SKIPS, None)
        s.commit()

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
        if created and r.status == "open":   # one for an older day than an open one is missed at once
            from .voice import calls
            rang = None
            if calls.options(ctx.session)["checkin"]:   # a real question, but still only in a call window (v8 D)
                ok, _ = calls.may_call(ctx.session, importance="normal", question=True, now=dt.datetime.now(self.profile.tz))
                if ok:
                    rang = calls.start(ctx.session, "Evening check-in",
                                       "Hi, it's Kairo. Time for your evening check-in. How did today go, and how is your energy, one to five?",
                                       self.pusher)
            if rang is None:
                self.pusher.notify("Evening check-in", "2 minutes: how did today go?", f"/#reviews/{r.id}", source="review")

    def _big_review_body(self, ctx: JobContext, day: dt.date | None = None) -> None:
        day = day or self.today()
        kind = reviews.big_kind_due(day)
        if not kind:
            raise Skipped("no review due today")
        r, created = reviews.create(ctx.session, kind, day, self.profile.tz)
        if created and r.status == "open":
            self.pusher.notify(reviews.TITLES[kind], f"About {reviews.MINUTES[kind]} minutes. Kairo filled in what it knows.",
                               f"/#reviews/{r.id}", source="review")

    # Old entry points (tests, catch-up); reviews are created even late in the evening.
    def daily_review(self, day: dt.date | None = None) -> None:
        self.run_job("review.daily", forced=True, day=day)

    def big_review(self, day: dt.date | None = None) -> None:
        if reviews.big_kind_due(day or self.today()):
            self.run_job("review.big", forced=True, day=day)

    def catch_up(self, now: dt.datetime | None = None) -> None:
        """Kairo was off (or restarting) at a scheduled time: run what matters now (fix plan item 23)."""
        now = (now or self.now()).astimezone(self.profile.tz)
        try:   # reviews that stacked up while Kairo was off (or before the one-open-per-kind rule) are missed
            with Session(self.engine) as s:
                reviews.tidy(s, now.date())
        except Exception:
            log.exception("review cleanup failed")
        if now.strftime("%H:%M") >= self.times["big"]:
            self.big_review(now.date())
        if now.strftime("%H:%M") >= self.times["daily"]:
            self.daily_review(now.date())
        for name in self.missed(now):
            try:
                self.run_job(name)
            except Exception:   # one failed catch-up never stops the rest
                log.exception("catch-up of %s failed", name)

    CATCH_UP_EVERY_MIN = {"email_triage": 60, "open_loops": LOOPS_EVERY_MIN, "pa": PA_EVERY_MIN}

    def missed(self, now: dt.datetime) -> list[str]:
        """The brief, if it's before 10:00 and today's is missing; triage, open loops and the PA if their
        last run is older than their interval. Nothing in the quiet hours (they'd only be skipped)."""
        if self.in_quiet(now):
            return []
        out = []
        with Session(self.engine) as s:
            def last(name):
                row = s.exec(select(JobRun).where(JobRun.job == name).where(JobRun.status.in_(("ok", "skipped", "error")))
                             .order_by(JobRun.started_at.desc())).first()
                return _aware(row.started_at) if row else None
            if "morning_brief" in self.bodies and now.hour < 10:
                key = "morning_brief.weekends" if now.weekday() >= 5 else "morning_brief.weekdays"
                minute, hour = self._cron(key).split()[:2]
                due = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0) if hour.isdigit() and minute.isdigit() else None
                ran = last("morning_brief")
                if due and due <= now and (ran is None or ran < due):
                    out.append("morning_brief")
            for name, every in self.CATCH_UP_EVERY_MIN.items():
                if name not in self.bodies or (name == "email_triage" and not 7 <= now.hour <= 21):
                    continue
                ran = last(name)
                if ran is None or now - ran > dt.timedelta(minutes=every):
                    out.append(name)
        return out

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
        if "knowledge_call" in self.bodies:   # checks every 30 min; calls at most once a day, at a good moment
            sched.add_job(self.run_job, IntervalTrigger(minutes=30, timezone=tz), args=["knowledge_call"],
                          id="knowledge_call", misfire_grace_time=600)
        if "pa" in self.bodies:
            every = ((self.schedule.get("jobs") or {}).get("pa") or {}).get("every_minutes") or PA_EVERY_MIN
            sched.add_job(self.run_job, IntervalTrigger(minutes=int(every), timezone=tz), args=["pa"], id="pa",
                          misfire_grace_time=900)
        if "voice_agenda" in self.bodies:   # local voice: Claude's call topics, ready before a call starts
            sched.add_job(self.run_job, IntervalTrigger(minutes=30, timezone=tz), args=["voice_agenda"],
                          id="voice_agenda", misfire_grace_time=900)
        if "coach" in self.bodies:
            every = ((self.schedule.get("jobs") or {}).get("coach") or {}).get("every_minutes") or COACH_EVERY_MIN
            sched.add_job(self.run_job, IntervalTrigger(minutes=int(every), timezone=tz), args=["coach"], id="coach",
                          misfire_grace_time=900)
        for name in cron_jobs(self.schedule):
            if name in self.bodies:
                sched.add_job(self.run_job, cron_trigger(self._cron(name), tz), args=[name], id=name)
        if "capture_process" in self.bodies:
            every = ((self.schedule.get("jobs") or {}).get("capture_process") or {}).get("every_minutes") or CAPTURE_EVERY_MIN
            sched.add_job(self.run_job, IntervalTrigger(minutes=int(every), timezone=tz), args=["capture_process"],
                          id="capture_process", misfire_grace_time=600)
        sched.start()
        self.scheduler = sched
        threading.Thread(target=self.catch_up, name="jobs-catch-up", daemon=True).start()

    def reload(self) -> None:
        """Settings changed (quiet hours, brief or review times): recompute and re-plan the jobs."""
        self.times = review_times(self.schedule)
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None
            self.start()

    def stop(self) -> None:
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
