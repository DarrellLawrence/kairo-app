"""Scheduled jobs (v5 Milestone M: reviews; Milestone C adds triage, brief, capture).

APScheduler in the backend process, in the owner's timezone. A run that was
missed (PC off or restarting) still happens when Kairo is back, within the
grace time; on start, today's reviews are caught up too. Every run is logged
in job_runs.
"""

from __future__ import annotations

import datetime as dt
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlmodel import Session

from . import reviews
from .db import JobRun, utcnow

log = logging.getLogger("kairo.scheduler")

DEFAULT_TIMES = {"daily": "21:00", "big": "18:00"}
GRACE_S = 6 * 3600


def review_times(schedule: dict | None) -> dict[str, str]:
    """`reviews: {daily: "21:00", big: "18:00"}` in schedule.yaml, defaults otherwise."""
    times = dict(DEFAULT_TIMES)
    for k, v in ((schedule or {}).get("reviews") or {}).items():
        if k in times and isinstance(v, str) and len(v) == 5 and v[2] == ":":
            times[k] = v
    return times


class Jobs:
    def __init__(self, *, engine, pusher, profile, schedule: dict | None = None):
        self.engine = engine
        self.pusher = pusher
        self.profile = profile
        self.times = review_times(schedule)
        self.scheduler: BackgroundScheduler | None = None

    def today(self) -> dt.date:
        return dt.datetime.now(self.profile.tz).date()

    def _run(self, name: str, fn) -> None:
        with Session(self.engine) as s:
            run = JobRun(job=name)
            s.add(run)
            s.commit()
            try:
                fn(s)
                run.status = "ok"
            except Exception as exc:   # a failed job is logged, never crashes Kairo
                log.exception("job %s failed", name)
                run.status, run.error = "error", str(exc)[:2000]
            run.finished_at = utcnow()
            s.add(run)
            s.commit()

    def daily_review(self, day: dt.date | None = None) -> None:
        day = day or self.today()

        def go(s):
            r, created = reviews.create(s, "daily", day)
            if created:
                self.pusher.notify("Evening check-in", "2 minutes: how did today go?", f"/#reviews/{r.id}")
        self._run("review.daily", go)

    def big_review(self, day: dt.date | None = None) -> None:
        day = day or self.today()
        kind = reviews.big_kind_due(day)
        if not kind:
            return

        def go(s):
            r, created = reviews.create(s, kind, day)
            if created:
                self.pusher.notify(reviews.TITLES[kind], f"About {reviews.MINUTES[kind]} minutes. Kairo filled in what it knows.",
                                   f"/#reviews/{r.id}")
        self._run(f"review.{kind}", go)

    def catch_up(self, now: dt.datetime | None = None) -> None:
        """Kairo was off at the scheduled time today: create the review now."""
        now = (now or dt.datetime.now(self.profile.tz)).astimezone(self.profile.tz)
        if now.strftime("%H:%M") >= self.times["big"]:
            self.big_review(now.date())
        if now.strftime("%H:%M") >= self.times["daily"]:
            self.daily_review(now.date())

    def start(self) -> None:
        sched = BackgroundScheduler(timezone=self.profile.tz,
                                    job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": GRACE_S})
        dh, dm = self.times["daily"].split(":")
        bh, bm = self.times["big"].split(":")
        sched.add_job(self.daily_review, CronTrigger(hour=int(dh), minute=int(dm), timezone=self.profile.tz), id="review.daily")
        sched.add_job(self.big_review, CronTrigger(day_of_week="sun", hour=int(bh), minute=int(bm), timezone=self.profile.tz), id="review.big")
        sched.start()
        self.scheduler = sched
        self.catch_up()

    def stop(self) -> None:
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
