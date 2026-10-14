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

from . import location, triage, vault
from .db import Approval, Brief, Capture, Checkin, Review, SchoolEvent, Task
from .google.client import GoogleError, GoogleNotConnected
from .jobkit import JOB_RULES, clip, extract_json
from .scheduler import JobContext, Skipped
from .tools import untrusted

log = logging.getLogger("kairo.brief")

PROMPT = """Write the owner's morning brief for {day} ({weekday}).
From the data below return JSON:
{{"headline": "one sentence: what matters most today",
  "top3": [{{"title": "...", "why": "one short reason"}}, ... up to 3]}}
Pick the top 3 from tasks, deadlines, school events and mails that need a reply, using
the owner's goals. "patterns" are facts from the owner's places (arrive/leave times); mention
one only if it matters today (e.g. a long school day and training planned). Write in the owner's language ({language}). Be concrete and short.

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
        tz = self.profile.tz
        start = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
        try:
            g = self.google_read()
        except GoogleNotConnected as exc:
            return [], f"Google not connected: {exc}"
        try:
            data = g.get("/calendar/v3/calendars/primary/events", timeMin=start.isoformat(),
                         timeMax=(start + dt.timedelta(days=2)).isoformat(), singleEvents="true",
                         orderBy="startTime", maxResults=30)
        except (GoogleNotConnected, GoogleError) as exc:
            return [], str(exc)
        finally:
            g.close()
        events = []
        for e in data.get("items", []) or []:
            st, en = e.get("start") or {}, e.get("end") or {}
            events.append({"summary": clip(e.get("summary") or "(no title)", 120),
                           "start": st.get("dateTime") or st.get("date"), "end": en.get("dateTime") or en.get("date"),
                           "all_day": "date" in st and "dateTime" not in st, "location": clip(e.get("location"), 120) or None})
        return events, None

    def gather(self, s: Session, day: dt.date) -> dict:
        tz = self.profile.tz
        week_end = dt.datetime.combine(day + dt.timedelta(days=7), dt.time.max, tzinfo=tz)
        day_start = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
        events, cal_error = self.calendar(day)
        open_tasks = s.exec(select(Task).where(Task.status == "open")).all()

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
            "school": [{"subject": e.subject, "kind": e.kind, "date": e.date.isoformat()} for e in school],
            "needs_reply": [triage.as_dict(m) for m in triage.needs_reply(s)],
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
        }

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
        data = self.gather(s, day)
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
        ctx.jobs.pusher.notify("Morning brief", written["headline"], "/#today")
        return by


def _data_for_model(data: dict) -> str:
    own = {k: data[k] for k in ("overdue", "next7", "other_tasks", "school", "checkin", "approvals", "place_tasks", "patterns")}
    outside = {"calendar": data["calendar"], "mails_needing_reply": [{"from": m["from"], "subject": m["subject"],
               "summary": m["summary"], "deadline": m["deadline"]} for m in data["needs_reply"]]}
    return ("Kairo's own data:\n" + json.dumps(own, ensure_ascii=False, default=str) + "\n\n"
            + untrusted("calendar and mail", outside))


def render_markdown(c: dict) -> str:
    lines = [f"# Morning brief {c['date']}", "", f"**{c['headline']}**", ""]
    if c.get("top3"):
        lines += ["## Top 3", *[f"{i}. {t['title']} ({t['why']})" for i, t in enumerate(c["top3"], 1)], ""]
    if c.get("calendar"):
        lines += ["## Calendar", *[f"- {(e['start'] or '')[11:16] or 'all day'} {e['summary']}" for e in c["calendar"]], ""]
    if c.get("needs_reply"):
        lines += ["## Needs a reply", *[f"- {m['from']}: {m['subject']} ({m['summary']})" for m in c["needs_reply"]], ""]
    if c.get("next7"):
        lines += ["## Next 7 days", *[f"- {t['due'][:10]} {t['title']}" for t in c["next7"]], ""]
    if c.get("approvals"):
        lines += [f"## Waiting for you: {len(c['approvals'])} approval(s)", ""]
    return "\n".join(lines)


def latest(s: Session, day: dt.date) -> dict | None:
    row = s.exec(select(Brief).where(Brief.date <= day).order_by(Brief.date.desc())).first()
    return None if row is None else json.loads(row.content_json)
