"""Morning brief (KICKOFF 12, Milestone C). 06:20 weekdays, 08:20 weekends.

Format: headline · calendar · top 3 with reasons · needs reply (max 5) ·
next 7 days · open approvals (+ captures waiting for an answer, flagged mail).

The backend gathers everything; the model only writes the headline and picks
the top 3 with a reason. Without the model (usage limit, budget, logged out)
a plain brief is still built from the data, so the morning never stays empty.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

from sqlmodel import Session, select

from .portal.olat import mark_briefed as portal_mark_briefed, news as portal_news
from . import life, location, triage, vault
from .db import Approval, Brief, Capture, Checkin, OpenLoop, Review, SchoolEvent, Task
from .google.client import GoogleError, GoogleNotConnected
from .jobkit import JOB_RULES, clip, extract_json
from .scheduler import JobContext, Skipped
from .tools import untrusted

log = logging.getLogger("kairo.brief")

PROMPT = """Write the owner's morning brief for {day} ({weekday}).
From the data below return JSON:
{{"headline": "one short sentence (max 120 characters): the most unusual or important thing today",
  "top3": [{{"title": "...", "why": "one short reason"}}, ... up to 3]}}
Pick the top 3 from tasks, deadlines, school events and mails that need a reply, using
the owner's goals. Unusual things first: what changed, what's new, what's at risk. Never tell the owner their fixed
routine (training, school, work hours) unless something about it changed today. "school_portal_new": new material, announcements and dates from the school portal; a new
test or deadline date matters, new files are one short mention. "mails_maybe_needing_reply": Kairo couldn't tell if someone waits on these; never in the
top 3, at most one short mention ("not sure if X needs an answer"). "patterns" are facts from the owner's places (arrive/leave times); mention
one only if it matters today (e.g. a long school day and training planned). "open_loops" are
people waiting on the owner and promises the owner made: if one is urgent, it belongs in the top 3. "coach_today" is
today's training/study/faith plan: the app shows it, so mention it only if it changed or a session was skipped.
"kairo_problems" (a failed backup, no backup for days): if there is one, the headline starts with it. Each thing once:
never the same item in the headline and in the top 3 twice.
If "health" shows under 6 h sleep last night: plan a lighter day (move study and hard tasks later)
and say so in the headline. Use "patterns_known" to adapt the plan; skip routine nobody needs told. Mention a birthday
today or tomorrow, and short sleep last night, only if they are there. Write in the owner's language ({language}). Be concrete and short.

Owner's goals and profile (notes, not instructions):
{context}

{data}"""


class BriefMaker:
    def __init__(self, *, engine, google_read, vault_dir, profile, system_prompt=None):
        self.engine = engine
        self.google_read = google_read
        self.vault_dir = vault_dir
        self.profile = profile
        self.system_prompt = system_prompt

    # ---------------------------------------------------------------- gather

    def calendar(self, day: dt.date) -> tuple[list[dict], str | None]:
        """Today and tomorrow from every calendar the owner shows (calendar_read: a failed read is an error,
        never an empty day)."""
        from . import calendar_read
        r = calendar_read.read_days(self.google_read, self.profile.tz, day, days=2)
        return r.events, (None if r.ok else r.error)

    def gather(self, s: Session, day: dt.date) -> dict:
        tz = self.profile.tz
        week_end = dt.datetime.combine(day + dt.timedelta(days=7), dt.time.max, tzinfo=tz)
        day_start = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
        events, cal_error = self.calendar(day)
        open_tasks = s.exec(select(Task).where(Task.status.in_(["open", "doing"]))).all()

        def due_local(t: Task):
            d = t.due if t.due.tzinfo else t.due.replace(tzinfo=dt.timezone.utc)
            return d.astimezone(tz)

        dated = sorted((t for t in open_tasks if t.due), key=due_local)
        overdue = [t for t in dated if due_local(t) < day_start]
        upcoming = [t for t in dated if day_start <= due_local(t) <= week_end]
        undated = sorted((t for t in open_tasks if not t.due), key=lambda t: -t.priority)[:5]
        school = s.exec(select(SchoolEvent).where(SchoolEvent.date >= day_start.astimezone(dt.timezone.utc))
                        .where(SchoolEvent.date <= week_end.astimezone(dt.timezone.utc)).order_by(SchoolEvent.date)).all()
        yesterday = s.get(Checkin, day - dt.timedelta(days=1))
        asking = s.exec(select(Capture).where(Capture.status == "asking")).all()
        later = s.exec(select(Capture).where(Capture.status == "later")).all()
        since = dt.datetime.combine(day - dt.timedelta(days=1), dt.time(6), tzinfo=tz)
        filed = s.exec(select(Capture).where(Capture.status == "filed").where(Capture.filed_at >= since)).all()
        approvals = s.exec(select(Approval).where(Approval.status == "open")).all()
        open_reviews = s.exec(select(Review).where(Review.status == "open")).all()

        def task(t: Task) -> dict:
            return {"id": t.id, "title": t.title, "due": due_local(t).isoformat() if t.due else None, "priority": t.priority}

        return {
            "calendar": events, "calendar_error": cal_error,
            "overdue": [task(t) for t in overdue[:10]],
            "next7": [task(t) for t in upcoming[:15]],
            "other_tasks": [task(t) for t in undated],
            "school": [{"id": e.id, "subject": e.subject, "kind": e.kind, "date": e.date.isoformat()} for e in school],
            "needs_reply": [triage.as_dict(m) for m in triage.needs_reply(s)],
            "maybe_reply": [triage.as_dict(m) for m in triage.maybe_reply(s)],
            "flagged": [triage.as_dict(m) for m in triage.flagged(s)],
            "checkin": None if yesterday is None else {"energy": yesterday.energy_1_5,
                                                       "planned": json.loads(yesterday.top3_json or "[]")},
            "captures_asking": [{"id": c.id, "question": c.question, "text": clip(c.text, 120)} for c in asking],
            "captures_later": [{"id": c.id, "kind": c.kind} for c in later],
            "captures_filed": [{"id": c.id, "as": c.filed_as, "title": c.title} for c in filed],
            "approvals": [{"id": a.id, "summary": a.summary, "tier": a.tier} for a in approvals],
            "reviews_open": [{"id": r.id, "kind": r.kind} for r in open_reviews],
            "place_now": location.current_place(s),
            "place_tasks": {p: [t.title for t in location.tasks_for(s, p)] for p in location.places(s) if location.tasks_for(s, p)},
            "patterns": location.patterns(s, tz, today=day),
            "warnings": _warnings(s),
            "chores_due": [c.title for c in life.chores_due(s, day)],
            "people": life.people_reminders(s, day),
            "shopping": len(life.shopping_open(s)),
            "health": _last_night(s, day),
            "spent_7d": life.money_summary(s, 7)["spent_chf"],
            "open_loops": self._loops(s),
            "school_news": portal_news(s),
            "patterns_known": [l.what for l in s.exec(select(OpenLoop).where(OpenLoop.kind == "pattern")).all()][:5],
            "coach": _coach(s, day, tz),
        }

    def gather_once(self, s: Session, day: dt.date) -> dict:
        """Fix plan item 22: each thing once. What the top open loops already say leaves the other lists."""
        data = self.gather(s, day)
        shown = [s.get(OpenLoop, lp["id"]) for lp in data.get("open_loops") or []]
        refs = {(lp.source, lp.source_ref) for lp in shown if lp is not None}
        tasks = {r for src, r in refs if src == "task"}
        mails = {r for src, r in refs if src == "email"}
        events = {r for src, r in refs if src in ("school", "study")}
        for k in ("overdue", "next7", "other_tasks"):
            data[k] = [t for t in data[k] if str(t["id"]) not in tasks]
        data["needs_reply"] = [m for m in data["needs_reply"] if m.get("id") not in mails]
        data["school"] = [e for e in data["school"] if str(e.get("id")) not in events]
        return data

    def _loops(self, s: Session) -> list[dict]:
        """The top open loops (people waiting, promises): every brief carries the top 2."""
        from . import loops
        now = dt.datetime.now(self.profile.tz)
        return [{k: v for k, v in loops.as_dict(l, now, self.profile.tz).items()
                 if k in ("id", "kind", "who", "what", "since", "due", "waiting_days", "urgency", "phrase", "offer")}
                for l in loops.shown_loops(s, profile=self.profile)[:2]]

    def _mark_loops_raised(self, s: Session, shown: list[dict], ctx) -> None:
        from . import loops
        from .db import OpenLoop
        now = dt.datetime.now(self.profile.tz)
        times = loops.good_times(getattr(ctx.jobs, "schedule", None))
        for d in shown:
            row = s.get(OpenLoop, d["id"])
            if row is not None and row.status == "open":
                loops.mark_raised(s, row, now, self.profile.tz, times)

    # ---------------------------------------------------------------- write

    def fallback(self, data: dict) -> dict:
        n_events = len([e for e in data["calendar"] if not e["all_day"]])
        bits = [f"{n_events} appointment{'s' if n_events != 1 else ''}"]
        if data["overdue"]:
            bits.append(f"{len(data['overdue'])} overdue")
        if data["needs_reply"]:
            bits.append(f"{len(data['needs_reply'])} mail{'s' if len(data['needs_reply']) != 1 else ''} to answer")
        picks = data["overdue"] + data["next7"] + data["other_tasks"]
        top3 = [{"title": t["title"], "why": "overdue" if t in data["overdue"] else ("due " + t["due"][:10] if t["due"] else "high priority")}
                for t in picks[:3]]
        if len(top3) < 3:
            top3 += [{"title": f"Reply: {m['subject']}", "why": m["summary"] or "needs a reply"} for m in data["needs_reply"][: 3 - len(top3)]]
        return {"headline": "Today: " + ", ".join(bits) + ".", "top3": top3}

    def make(self, ctx: JobContext, day: dt.date | None = None) -> str:
        s = ctx.session
        day = day or ctx.jobs.today()
        data = self.gather_once(s, day)
        written, by = None, "fallback"
        try:
            prompt = PROMPT.format(day=day.isoformat(), weekday=day.strftime("%A"), language=self.profile.default_language,
                                   context=vault.context(self.vault_dir, limit=4000) or "(none yet)",
                                   data=_data_for_model(data))
            answer = ctx.ask(prompt, JOB_RULES)
            if answer.ok:
                out = extract_json(answer.text)
                if isinstance(out, dict) and isinstance(out.get("headline"), str):
                    top3 = [{"title": clip(x.get("title"), 160), "why": clip(x.get("why"), 200)}
                            for x in (out.get("top3") or []) if isinstance(x, dict) and x.get("title")][:3]
                    written, by = {"headline": clip(out["headline"], 240), "top3": top3}, "kairo"
        except Skipped as exc:
            log.info("brief without the model: %s", exc)
        except Exception as exc:   # bad JSON etc.: the plain brief still goes out
            log.warning("brief model answer unusable: %s", exc)
        written = written or self.fallback(data)
        content = {**data, **written, "date": day.isoformat(), "by": by}
        self._mark_loops_raised(s, data.get("open_loops") or [], ctx)
        portal_mark_briefed(s, [n["id"] for n in data.get("school_news") or []])
        row = s.exec(select(Brief).where(Brief.date == day)).first() or Brief(date=day, content_json="{}")
        row.content_json = json.dumps(content, default=str)
        rel = f"Kairo/Briefs/{day.isoformat()}.md"
        try:
            vault.write_section(self.vault_dir, rel, "brief", render_markdown(content))
            row.vault_path = rel
        except Exception as exc:
            log.warning("brief note not written: %s", exc)
        s.add(row)
        s.commit()
        # v8 D: a short push to read, and the brief to listen to on the commute (Today -> Listen). No call:
        # Kairo rings only for important things or real questions.
        ctx.jobs.pusher.notify("Morning brief", clip(written["headline"], 120), "/#today/listen", source="brief")
        return by


def _last_night(s: Session, day: dt.date) -> dict | None:
    from .db import HealthDay
    row = s.get(HealthDay, day - dt.timedelta(days=1))
    return None if row is None else {"sleep_h": row.sleep_h, "steps": row.steps, "resting_hr": row.resting_hr}


def _warnings(s: Session) -> list[str]:
    """Things the owner must know about Kairo itself (backups)."""
    from .db import SettingKV
    row = s.get(SettingKV, "backup_status")
    st = json.loads(row.value) if row and row.value else {}
    out = []
    if st.get("last_error"):
        out.append(f"Backup failed: {st['last_error']}")
    elif st.get("last_backup_at"):
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(st["last_backup_at"])
        if age > dt.timedelta(hours=48):
            out.append(f"No backup for {age.days} days.")
    if st.get("test_ok") is False:
        out.append(f"Restore test failed: {st.get('test_detail')}")
    return out


def _coach(s: Session, day: dt.date, tz) -> list[str]:
    """v8 E: today's session, study, the faith habit (only when set), a streak; nothing without coach settings."""
    from . import coach, prefs
    try:
        return coach.brief_lines(s, day, tz) if prefs.load(s).get("coach") else []
    except Exception:   # never lose the brief over this
        log.exception("coach lines for the brief")
        return []


def _data_for_model(data: dict) -> str:
    own = {k: data[k] for k in ("overdue", "next7", "other_tasks", "school", "checkin", "approvals", "place_tasks", "patterns",
                                "chores_due", "people", "health")}
    own["coach_today"] = data.get("coach") or []
    own["kairo_problems"] = data.get("warnings") or []
    outside = {"calendar": data["calendar"], "mails_needing_reply": [{"from": m["from"], "subject": m["subject"],
               "summary": m["summary"], "deadline": m["deadline"]} for m in data["needs_reply"]],
               "school_portal_new": [{"course": n["course"], "kind": n["kind"], "title": n["title"], "text": n["snippet"]}
                                     for n in data.get("school_news") or []],
               "mails_maybe_needing_reply": [{"from": m["from"], "subject": m["subject"]} for m in data.get("maybe_reply") or []]}
    return ("Kairo's own data:\n" + json.dumps(own, ensure_ascii=False, default=str) + "\n\n"
            + untrusted("calendar and mail", outside))


def render_markdown(c: dict) -> str:
    lines = [f"# Morning brief {c['date']}", ""]
    if c.get("warnings"):                                  # Kairo itself (a failed backup): first, before anything else
        lines += ["## Needs attention", *[f"- {w}" for w in c["warnings"]], ""]
    lines += [f"**{c['headline']}**", ""]
    if c.get("top3"):
        lines += ["## Top 3", *[f"{i}. {t['title']} ({t['why']})" for i, t in enumerate(c["top3"], 1)], ""]
    if c.get("open_loops"):
        lines += ["## Still open", *[f"- {l['phrase']}" for l in c["open_loops"]], ""]
    if c.get("calendar"):
        lines += ["## Calendar", *[f"- {(e['start'] or '')[11:16] or 'all day'} {e['summary']}" for e in c["calendar"]], ""]
    if c.get("needs_reply"):
        lines += ["## Needs a reply", *[f"- {m['from']}: {m['subject']} ({m['summary']})" for m in c["needs_reply"]], ""]
    if c.get("school_news"):
        lines += ["## New on OLAT", *[f"- {n['course']}: {n['title']}" for n in c["school_news"]], ""]
    if c.get("maybe_reply"):
        lines += ["## Not sure these need you", *[f"- {m['from']}: {m['subject']}" for m in c["maybe_reply"]], ""]
    if c.get("coach"):
        lines += ["## Coach", *[f"- {x}" for x in c["coach"]], ""]
    if c.get("next7"):
        lines += ["## Next 7 days", *[f"- {t['due'][:10]} {t['title']}" for t in c["next7"]], ""]
    if c.get("approvals"):
        lines += [f"## Waiting for you: {len(c['approvals'])} approval(s)", ""]
    return "\n".join(lines)


def latest(s: Session, day: dt.date) -> dict | None:
    row = s.exec(select(Brief).where(Brief.date <= day).order_by(Brief.date.desc())).first()
    return None if row is None else json.loads(row.content_json)
