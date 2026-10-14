"""Open loops: Kairo never forgets and never waits to be asked.

A loop is anything unfinished: a mail or question waiting for the owner, a
promise the owner made ("I'll send it tomorrow"), a deadline, a task. Kairo
keeps them in `open_loops`, raises the most urgent ones itself with a ready
answer (drafted in the owner's voice), and brings them back until they are
done, dropped, or no longer relevant (e.g. the owner already answered the
thread in Gmail).

Snooze ("Later", "not now", or no reaction) by urgency:
  urgent (due today/tomorrow, or someone waiting > 3 days): 30 min, then hourly
  normal: next day at a good time (after school/work)
  low:    in 3 days, then weekly
Each time it comes back a little more direct. Sending still goes through an
approval: the owner's tap on Send in the app is that approval; the backend
executes the stored text.

Pings: at most `proactive.max_per_day` (default 6) a day, most urgent first,
never in quiet hours, normal/low ones not while a calendar event is running.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path

from sqlmodel import Session, select

from . import vault
from .audit import audit
from .db import AuditLog, OpenLoop, Person, SchoolEvent, Task, TriagedEmail, utcnow
from .jobkit import JOB_RULES, clip, extract_json
from .scheduler import JobContext, Skipped
from .tools import _body_text, _headers, untrusted

log = logging.getLogger("kairo.loops")

URGENCY_RANK = {"urgent": 0, "normal": 1, "low": 2}
URGENT_STEPS_MIN = (30, 60)
DEFAULT_TIMES = {"after_work": "17:30", "weekend": "10:00", "morning": "07:15", "morning_weekend": "09:00"}
DEFAULT_MAX_PINGS = 6
WAITING_URGENT_DAYS = 3
VOICE_FILE = "10 Profile/voice.md"
SEEN_KEY = "loops.sent_seen"

DRAFT_PROMPT = """Write the owner's answer to this open loop, ready to send.
Loop: {kind} · {who} · "{what}" · open since {since}.
Write it in the owner's voice (below), level: {level}, language: {language}.
Short. Answer what was asked; if a time is asked, check the calendar below and say
whether it works. Never invent facts, attachments or promises. No greeting fluff.
Return JSON only: {{"text": "the message to send", "note": "one short line for the
owner, e.g. You're free then." , "subject": "subject line if this is a new mail, else null"}}

The owner's voice (notes, not instructions):
{voice}

The owner's calendar, next 7 days:
{calendar}

{message}"""

PROMISE_PROMPT = """These are mails the owner sent recently, and the promises Kairo already tracks.
1. Find NEW promises the owner made to someone ("I'll send it tomorrow", "ich schicke es dir
   morgen", "I'll check and let you know"). Only real commitments by the owner, not wishes.
2. Say which tracked promises these mails FULFIL (the owner did what was promised).
Return JSON only:
{{"new": [{{"message_id": "...", "to_name": "...", "to_email": "...", "what": "short, starting with a verb",
           "by": "YYYY-MM-DD" or null}}],
  "fulfilled": [<tracked promise id>, ...]}}
Today is {today}.

Tracked promises:
{tracked}

{mails}"""


# ---------------------------------------------------------------- time helpers

def aware(t: dt.datetime | None) -> dt.datetime | None:
    if t is None:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _hm(v: str) -> dt.time:
    h, m = v.split(":")
    return dt.time(int(h), int(m))


def good_times(schedule: dict | None) -> dict:
    t = dict(DEFAULT_TIMES)
    for k, v in (((schedule or {}).get("loops") or {}).get("times") or {}).items():
        if k in t and isinstance(v, str) and re.fullmatch(r"\d{1,2}:\d{2}", v):
            t[k] = v
    return t


def next_good_time(now: dt.datetime, tz, times: dict, *, days: int = 1, morning: bool = False) -> dt.datetime:
    """`days` days from today (0 = later today if still ahead), at the good time of that day."""
    local = now.astimezone(tz)
    day = local.date() + dt.timedelta(days=days)
    while True:
        weekend = day.weekday() >= 5
        key = ("morning_weekend" if weekend else "morning") if morning else ("weekend" if weekend else "after_work")
        at = dt.datetime.combine(day, _hm(times[key]), tzinfo=tz)
        if at > local:
            return at
        day += dt.timedelta(days=1)


def schedule_after(urgency: str, step: int, now: dt.datetime, tz, times: dict) -> dt.datetime:
    """When a loop comes back after its `step`-th deferral (0 = first)."""
    if urgency == "urgent":
        return now + dt.timedelta(minutes=URGENT_STEPS_MIN[min(step, len(URGENT_STEPS_MIN) - 1)])
    if urgency == "low":
        return next_good_time(now, tz, times, days=3 if step == 0 else 7)
    return next_good_time(now, tz, times, days=1)


def urgency_of(loop: OpenLoop, now: dt.datetime, tz) -> str:
    due = aware(loop.due)
    if due is not None:
        days = (due.astimezone(tz).date() - now.astimezone(tz).date()).days
        if days <= 1:
            return "urgent"
        if days <= 7:
            return "normal"
    if loop.kind in ("reply", "waiting", "promise"):
        waited = now - aware(loop.since)
        return "urgent" if waited > dt.timedelta(days=WAITING_URGENT_DAYS) else "normal"
    return "low" if loop.kind == "task" and due is None else "normal"


# ---------------------------------------------------------------- words

def _ago(since: dt.datetime, now: dt.datetime, tz) -> str:
    days = (now.astimezone(tz).date() - since.astimezone(tz).date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"on {since.astimezone(tz):%A}"
    return "last week" if days < 14 else f"{days} days ago"


def _when(due: dt.datetime, now: dt.datetime, tz) -> str:
    days = (due.astimezone(tz).date() - now.astimezone(tz).date()).days
    if days < 0:
        return f"{-days} day{'s' if days != -1 else ''} overdue" if days < -1 else "overdue since yesterday"
    if days == 0:
        return "due today"
    if days == 1:
        return "due tomorrow"
    return f"due {due.astimezone(tz):%A}" if days < 7 else f"due {due.astimezone(tz):%d %b}"


def phrase(loop: OpenLoop, now: dt.datetime, tz) -> str:
    """What Kairo says. A little more direct each time it comes back."""
    since = aware(loop.since)
    waited = (now.astimezone(tz).date() - since.astimezone(tz).date()).days
    who = loop.who or "Someone"
    again = loop.raised >= 1
    if loop.kind in ("reply", "waiting"):
        if again and waited >= 2:
            return f"{who}'s been waiting {waited} days now: {loop.what}."
        if again:
            return f"{who} is still waiting for your answer: {loop.what}."
        return f"{who} asked {_ago(since, now, tz)}: {loop.what}."
    if loop.kind == "promise":
        by = f" ({_when(aware(loop.due), now, tz)})" if loop.due else ""
        if again:
            return f"You promised {loop.who or 'it'} {_ago(since, now, tz)}: {loop.what}. Still open{by}."
        return f"You told {loop.who or 'someone'} you'd {loop.what}{by}."
    due = f", {_when(aware(loop.due), now, tz)}" if loop.due else ""
    return f"{loop.what}{due}. Still not done." if again else f"{loop.what}{due}."


def draft_of(loop: OpenLoop) -> dict | None:
    try:
        return json.loads(loop.draft_json) if loop.draft_json else None
    except ValueError:
        return None


def offer(loop: OpenLoop) -> str | None:
    d = draft_of(loop)
    if not d or not d.get("text"):
        return None
    first = d["text"].strip().splitlines()[0]
    return f"Reply ‘{clip(first, 70)}’?" if loop.kind in ("reply", "waiting") else f"Send ‘{clip(first, 70)}’?"


def as_dict(loop: OpenLoop, now: dt.datetime, tz) -> dict:
    d = draft_of(loop)
    since = aware(loop.since)
    return {"id": loop.id, "kind": loop.kind, "source": loop.source, "who": loop.who, "who_email": loop.who_email,
            "what": loop.what, "since": since.isoformat(), "due": aware(loop.due).isoformat() if loop.due else None,
            "waiting_days": (now.astimezone(tz).date() - since.astimezone(tz).date()).days,
            "urgency": loop.urgency, "status": loop.status, "raised": loop.raised,
            "next_at": aware(loop.next_at).isoformat() if loop.next_at else None,
            "phrase": phrase(loop, now, tz), "offer": offer(loop), "draft": d,
            "can_send": bool(d and d.get("text") and (loop.kind in ("reply", "waiting") and loop.source == "email"
                                                       or loop.who_email))}


def open_loops(s: Session) -> list[OpenLoop]:
    rows = s.exec(select(OpenLoop).where(OpenLoop.status == "open")).all()
    return sorted(rows, key=lambda r: (URGENCY_RANK.get(r.urgency, 1), aware(r.due) or dt.datetime.max.replace(tzinfo=dt.timezone.utc),
                                       aware(r.since)))


# ---------------------------------------------------------------- changes

class LoopError(ValueError):
    pass


def _get_open(s: Session, loop_id: int) -> OpenLoop:
    loop = s.get(OpenLoop, loop_id)
    if loop is None or loop.status != "open":
        raise LoopError(f"no open loop #{loop_id}")
    return loop


def _first_due(loop: OpenLoop, now: dt.datetime, tz, times: dict) -> dt.datetime:
    if loop.urgency == "urgent" or loop.kind == "reply":   # mails needing a reply: ping at once (PA rule 5)
        return now
    if loop.kind == "promise":                             # remind the next day (PA rule 10)
        return next_good_time(now, tz, times, days=1, morning=True)
    if loop.urgency == "low":
        return next_good_time(now, tz, times, days=3)
    return next_good_time(now, tz, times, days=0)


def upsert(s: Session, *, kind: str, source: str, ref: str | None, what: str, now: dt.datetime, tz, times: dict,
           who: str | None = None, who_email: str | None = None, since: dt.datetime | None = None,
           due: dt.datetime | None = None) -> tuple[OpenLoop, bool]:
    loop = s.exec(select(OpenLoop).where(OpenLoop.source == source).where(OpenLoop.source_ref == ref)).first() if ref else None
    created = loop is None
    if created:
        loop = OpenLoop(kind=kind, source=source, source_ref=ref, since=since or now)
    if loop.status != "open":
        return loop, False                       # done / dropped stays closed
    loop.what, loop.who, loop.who_email, loop.due = clip(what, 300), who, who_email, due
    old = loop.urgency
    loop.urgency = urgency_of(loop, now, tz)
    if created:
        loop.next_at = _first_due(loop, now, tz, times)
    elif loop.urgency == "urgent" and old != "urgent":
        loop.next_at = min(aware(loop.next_at) or now, now)   # it just became urgent: raise it now
    s.add(loop)
    s.commit()
    s.refresh(loop)
    return loop, created


def add(s: Session, *, kind: str, what: str, who: str | None, due: dt.date | None, now: dt.datetime, tz, times: dict) -> OpenLoop:
    due_at = dt.datetime.combine(due, dt.time(18), tzinfo=tz) if due else None
    loop = OpenLoop(kind=kind, source="chat", source_ref=f"chat:{now.timestamp():.6f}", what=clip(what, 300), who=who,
                    since=now, due=due_at)
    loop.urgency = urgency_of(loop, now, tz)
    loop.next_at = _first_due(loop, now, tz, times)
    s.add(loop)
    s.commit()
    s.refresh(loop)
    return loop


CRITICAL_WINDOW = dt.timedelta(hours=3)     # due this soon: it can't move any more
IMPORTANT_WINDOW = dt.timedelta(hours=24)   # due this soon and pushed away twice: pushback


class Refused(LoopError):
    """A critical loop the owner tried to push away: Kairo says no, with the reason."""


def _hhmm(t: dt.datetime, now: dt.datetime, tz) -> str:
    t, n = t.astimezone(tz), now.astimezone(tz)
    if t.date() == n.date():
        return f"at {t:%H:%M}"
    if t.date() == n.date() + dt.timedelta(days=1):
        return f"tomorrow {t:%H:%M}"
    return f"{t:%A %H:%M}"


def snooze(s: Session, loop_id: int, *, now: dt.datetime, tz, times: dict, when: str | None = None,
           at: dt.datetime | None = None) -> tuple[OpenLoop, str]:
    """'Later' never means forgotten: it comes back by the urgency rules, or at the time asked.
    Important things get pushback with the preparation; critical ones can't move (Refused)."""
    loop = _get_open(s, loop_id)
    due = aware(loop.due)
    ready = " It's ready: just check it." if draft_of(loop) else ""
    if due is not None and due - now <= CRITICAL_WINDOW:
        raise Refused(f"No, this one can't move: {loop.what} is due {_hhmm(due, now, tz)}.{ready} Do it now.")
    counted = True
    if at is not None:
        nxt = aware(at)
        if nxt <= now:
            raise LoopError("that time has passed")
    elif when == "in_1h":
        nxt = now + dt.timedelta(hours=1)
    elif when == "tomorrow":
        nxt = next_good_time(now, tz, times, days=1, morning=True)
    elif when == "later":
        # A Later right after Kairo raised it answers that raise (already counted).
        just_raised = loop.last_raised_at and now - aware(loop.last_raised_at) < dt.timedelta(hours=2) and loop.snoozed > 0
        step = loop.snoozed - 1 if just_raised else loop.snoozed
        nxt = schedule_after(loop.urgency, step, now, tz, times)
        counted = not just_raised
    else:
        raise LoopError("when must be later, in_1h or tomorrow")
    if due is not None and nxt > due - dt.timedelta(minutes=30):
        nxt = max(now + dt.timedelta(minutes=15), due - dt.timedelta(hours=1))   # never past the deadline
    pushed = loop.snoozed
    if counted:
        loop.snoozed += 1
    loop.next_at = nxt
    s.add(loop)
    s.commit()
    s.refresh(loop)
    msg = f"OK, back {_hhmm(nxt, now, tz)}."
    if due is not None and due - now <= IMPORTANT_WINDOW and pushed >= 2:
        msg += f" But it's due {_hhmm(due, now, tz)}.{ready or ' Do it before then.'}"
    return loop, msg


def close(s: Session, loop_id: int, reason: str, *, now: dt.datetime | None = None) -> OpenLoop:
    loop = _get_open(s, loop_id)
    _close(loop, reason, now or utcnow())
    s.add(loop)
    s.commit()
    s.refresh(loop)
    return loop


def _close(loop: OpenLoop, reason: str, now: dt.datetime) -> None:
    loop.status = {"done": "done", "sent": "done", "answered": "done", "dropped": "dropped"}.get(reason, "stale")
    loop.close_reason = reason
    loop.closed_at = now
    loop.next_at = None


def mark_raised(s: Session, loop: OpenLoop, now: dt.datetime, tz, times: dict) -> None:
    """Raised (pinged or in the brief). If nothing happens it comes back by the rules."""
    loop.next_at = schedule_after(loop.urgency, loop.snoozed, now, tz, times)
    loop.snoozed += 1
    loop.raised += 1
    loop.last_raised_at = now
    s.add(loop)
    s.commit()


# ---------------------------------------------------------------- where loops come from

def _name(sender: str | None) -> str | None:
    if not sender:
        return None
    name = re.sub(r"<[^>]*>", "", sender).strip().strip('"')
    return clip(name, 60) or None


def _email(sender: str | None) -> str | None:
    m = re.search(r"<([^>]+)>", sender or "")
    return (m.group(1) if m else (sender or "")).strip().lower() or None


def sync(s: Session, *, now: dt.datetime, tz, times: dict, answered=None) -> dict:
    """Bring the table in line with mail triage, tasks and school dates.
    `answered(thread_id, since) -> bool | None`: has the owner already replied in Gmail?"""
    counts = {"new": 0, "closed": 0}
    since_cut = utcnow() - dt.timedelta(days=30)
    for m in s.exec(select(TriagedEmail).where(TriagedEmail.category == "reply")
                    .where(TriagedEmail.triaged_at >= since_cut)).all():
        if m.suspicious:
            continue
        due = dt.datetime.combine(m.deadline, dt.time(18), tzinfo=tz) if m.deadline else None
        if m.done:
            existing = s.exec(select(OpenLoop).where(OpenLoop.source == "email").where(OpenLoop.source_ref == m.message_id)).first()
            if existing and existing.status == "open":
                _close(existing, "done", now)
                s.add(existing)
                s.commit()
                counts["closed"] += 1
            continue
        what = clip(m.summary or m.subject or "a mail", 200)
        _, created = upsert(s, kind="reply", source="email", ref=m.message_id, what=what, who=_name(m.sender),
                            who_email=_email(m.sender), since=aware(m.received_at) or aware(m.triaged_at), due=due,
                            now=now, tz=tz, times=times)
        counts["new"] += created

    horizon = now + dt.timedelta(days=7)
    tasks = {str(t.id): t for t in s.exec(select(Task)).all()}
    for tid, t in tasks.items():
        due = aware(t.due)
        if t.status in ("open", "doing") and ((due and due <= horizon) or (not due and t.priority >= 2)):
            _, created = upsert(s, kind="deadline" if due else "task", source="task", ref=tid, what=t.title, due=due,
                                since=aware(t.created_at), now=now, tz=tz, times=times)
            counts["new"] += created
    for e in s.exec(select(SchoolEvent).where(SchoolEvent.date >= utcnow() - dt.timedelta(days=1))
                    .where(SchoolEvent.date <= horizon)).all():
        if e.kind in ("test", "deadline"):
            _, created = upsert(s, kind="deadline", source="school", ref=str(e.id), what=f"{e.subject} {e.kind}",
                                due=aware(e.date), since=now, now=now, tz=tz, times=times)
            counts["new"] += created

    for loop in s.exec(select(OpenLoop).where(OpenLoop.status == "open")).all():
        reason = None
        if loop.source == "task":
            t = tasks.get(loop.source_ref or "")
            if t is None or t.status in ("done", "dropped", "archived"):
                reason = "done" if t is not None and t.status == "done" else "stale"
        elif loop.source == "school":
            e = s.get(SchoolEvent, int(loop.source_ref)) if (loop.source_ref or "").isdigit() else None
            if e is None or aware(e.date) < now - dt.timedelta(hours=12):
                reason = "stale"
        elif loop.source == "email" and answered is not None:
            m = s.get(TriagedEmail, loop.source_ref)
            try:
                if m is not None and m.thread_id and answered(m.thread_id, aware(loop.since)):
                    reason = "answered"
            except Exception as exc:   # Gmail down: check again next time
                log.info("answered check failed: %s", exc)
        if reason:
            _close(loop, reason, now)
            s.add(loop)
            s.commit()
            counts["closed"] += 1
            continue
        u = urgency_of(loop, now, tz)
        if u != loop.urgency:
            loop.urgency = u
            if u == "urgent":
                loop.next_at = min(aware(loop.next_at) or now, now)
            s.add(loop)
            s.commit()
    return counts


# ---------------------------------------------------------------- the ping budget (shared by everything proactive)

def pings_today(s: Session, now: dt.datetime, tz) -> int:
    start = dt.datetime.combine(now.astimezone(tz).date(), dt.time.min, tzinfo=tz).astimezone(dt.timezone.utc)
    return len(s.exec(select(AuditLog.id).where(AuditLog.action == "proactive.ping")
                      .where(AuditLog.ts >= start)).all())


def max_pings(schedule: dict | None) -> int:
    try:
        return int(((schedule or {}).get("proactive") or {}).get("max_per_day") or DEFAULT_MAX_PINGS)
    except (TypeError, ValueError):
        return DEFAULT_MAX_PINGS


# ---------------------------------------------------------------- the owner's voice

def voice_text(vault_dir: Path, config_dir: Path, profile) -> str:
    """vault/10 Profile/voice.md; made once from config/voice.md, then the owner's to edit."""
    path = Path(vault_dir) / VOICE_FILE
    if not path.is_file():
        tpl = Path(config_dir) / "voice.md"
        if tpl.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tpl.read_text(encoding="utf-8").replace("{{name}}", profile.name.split()[0]), encoding="utf-8")
    try:
        return path.read_text(encoding="utf-8")[:4000]
    except OSError:
        return ""


PRO_HINTS = re.compile(r"\b(mr|mrs|ms|dr|herr|frau|prof)\b\.?", re.I)
FREEMAIL = ("gmail.", "outlook.", "hotmail.", "icloud.", "gmx.", "bluewin.", "yahoo.", "proton")


def level_for(s: Session, who: str | None, email: str | None) -> str:
    """casual for friends and family, professional for school/work/strangers with a title, improved otherwise."""
    if who:
        p = s.exec(select(Person).where(Person.name == who)).first()
        if p and (p.relation or "").lower() in ("friend", "family", "partner", "brother", "sister", "mother", "father", "mum", "dad"):
            return "casual"
    if PRO_HINTS.search(who or "") or (email and not any(f in email for f in FREEMAIL)):
        return "professional"
    return "improved"


# ---------------------------------------------------------------- the job

class LoopEngine:
    """Job body 'open_loops' (every 15 minutes in the day): sync, then raise the most urgent one."""

    def __init__(self, *, engine, profile, schedule, google_read, calendar, pusher, vault_dir, config_dir):
        self.engine, self.profile, self.schedule = engine, profile, schedule or {}
        self.google_read, self.calendar, self.pusher = google_read, calendar, pusher
        self.vault_dir, self.config_dir = vault_dir, config_dir

    @property
    def times(self) -> dict:
        return good_times(self.schedule)

    def answered(self, thread_id: str, since: dt.datetime) -> bool:
        """Did the owner already answer in this Gmail thread (from the phone, the PC, anywhere)?"""
        owner = (self.profile.gmail or "").lower()
        if not owner:
            return False
        g = self.google_read()
        try:
            th = g.get(f"/gmail/v1/users/me/threads/{thread_id}", format="metadata", metadataHeaders=["From"])
        finally:
            g.close()
        for m in th.get("messages", []) or []:
            when = dt.datetime.fromtimestamp(int(m.get("internalDate", "0")) / 1000, dt.timezone.utc)
            if when > since and owner in (_headers(m).get("from") or "").lower():
                return True
        return False

    def run(self, ctx: JobContext, now: dt.datetime | None = None) -> str:
        tz = self.profile.tz
        now = (now or dt.datetime.now(tz)).astimezone(tz)
        s = ctx.session
        counts = sync(s, now=now, tz=tz, times=self.times, answered=self.answered)
        due = [l for l in open_loops(s) if l.next_at is None or aware(l.next_at) <= now]
        if not due:
            return json.dumps({**counts, "raised": None})
        from .push import in_quiet_hours
        if in_quiet_hours(self.profile, now):
            raise Skipped("quiet hours")
        fresh_replies = [l for l in due if l.kind == "reply" and l.raised == 0]   # always pinged at once (PA rule 5)
        if pings_today(s, now, tz) >= max_pings(self.schedule) and not fresh_replies:
            return json.dumps({**counts, "raised": None, "why": "daily ping limit"})
        if pings_today(s, now, tz) >= max_pings(self.schedule):
            due = fresh_replies
        busy = self._busy(now)
        pick = next((l for l in due if l.urgency == "urgent" or l in fresh_replies or not busy), None)
        if pick is None:
            return json.dumps({**counts, "raised": None, "why": "in a calendar event"})
        if pick.kind in ("reply", "waiting", "promise") and draft_of(pick) is None:
            try:
                self.draft(ctx, pick)
            except Exception as exc:   # no model right now: raise it anyway, without a ready answer
                log.info("no draft for loop %s: %s", pick.id, exc)
        body = phrase(pick, now, tz)
        if offer(pick):
            body += " " + offer(pick)
        more = len(due) - 1
        if more:
            body += f" (+{more} more)"
        self.pusher.notify("Kairo", body, f"/#loops/{pick.id}")
        audit(s, actor="backend", action="proactive.ping", outcome=f"loop #{pick.id}: {clip(body, 160)}")
        mark_raised(s, pick, now, tz, self.times)
        return json.dumps({**counts, "raised": pick.id})

    def _busy(self, now: dt.datetime) -> bool:
        try:
            events, _ = self.calendar(now.date())
        except Exception:
            return False
        for e in events:
            if e.get("all_day") or not e.get("start") or not e.get("end"):
                continue
            try:
                if dt.datetime.fromisoformat(e["start"]) <= now < dt.datetime.fromisoformat(e["end"]):
                    return True
            except ValueError:
                continue
        return False

    def draft(self, ctx: JobContext, loop: OpenLoop) -> dict | None:
        """The ready answer, in the owner's voice. Mail content is untrusted data."""
        s = ctx.session
        message = f"What it's about: {loop.what}"
        language = self.profile.default_language
        if loop.source == "email" and loop.source_ref:
            g = self.google_read()
            try:
                msg = g.get(f"/gmail/v1/users/me/messages/{loop.source_ref}", format="full")
            finally:
                g.close()
            body = clip(_body_text(msg.get("payload") or {}), 6000)
            h = _headers(msg)
            message = untrusted(f"the email from {h.get('from', '?')}", {"subject": h.get("subject"), "body": body})
            try:
                from .voice import language_of
                language = language_of(body) or language
            except Exception:
                pass
        events, _ = self.calendar(dt.datetime.now(self.profile.tz).date())
        cal = "\n".join(f"- {e['start']}–{e.get('end') or ''} {e['summary']}" for e in events[:30]) or "(nothing)"
        prompt = DRAFT_PROMPT.format(kind=loop.kind, who=loop.who or "?", what=loop.what,
                                     since=aware(loop.since).date().isoformat(), level=level_for(s, loop.who, loop.who_email),
                                     language=language, voice=voice_text(self.vault_dir, self.config_dir, self.profile) or "(none)",
                                     calendar=cal, message=message)
        answer = ctx.ask(prompt, JOB_RULES, purpose="draft")
        if not answer.ok:
            raise RuntimeError(answer.error or answer.status)
        out = extract_json(answer.text)
        if not isinstance(out, dict) or not str(out.get("text") or "").strip():
            raise ValueError("no draft in the answer")
        d = {"text": str(out["text"]).strip()[:4000], "note": clip(out.get("note"), 160),
             "subject": clip(out.get("subject"), 200) or None, "level": level_for(s, loop.who, loop.who_email),
             "language": language}
        loop.draft_json = json.dumps(d, ensure_ascii=False)
        s.add(loop)
        s.commit()
        return d


class PromiseFinder:
    """Job body 'promises' (hourly): the owner's sent mail -> new promise loops, fulfilled ones closed."""

    def __init__(self, *, profile, schedule, google_read):
        self.profile, self.schedule, self.google_read = profile, schedule or {}, google_read

    def run(self, ctx: JobContext, now: dt.datetime | None = None) -> str:
        from .db import SettingKV
        tz = self.profile.tz
        now = (now or dt.datetime.now(tz)).astimezone(tz)
        s = ctx.session
        row = s.get(SettingKV, SEEN_KEY) or SettingKV(key=SEEN_KEY, value="[]")
        seen = set(json.loads(row.value or "[]"))
        g = self.google_read()
        try:
            listing = g.get("/gmail/v1/users/me/messages", q="in:sent newer_than:2d", maxResults=15)
            mails = []
            for m in listing.get("messages", []) or []:
                if m["id"] in seen:
                    continue
                full = g.get(f"/gmail/v1/users/me/messages/{m['id']}", format="full")
                h = _headers(full)
                body = _body_text(full.get("payload") or {})
                body = re.split(r"\n(?:On .{5,80} wrote:|Am .{5,80} schrieb.*:|>)", body)[0]   # drop the quoted mail
                mails.append({"message_id": full["id"], "to": h.get("to"), "subject": h.get("subject"), "date": h.get("date"),
                              "text": clip(body, 1500)})
        finally:
            g.close()
        if not mails:
            raise Skipped("no new sent mail")
        tracked = [{"id": l.id, "who": l.who, "what": l.what} for l in
                   s.exec(select(OpenLoop).where(OpenLoop.status == "open").where(OpenLoop.kind == "promise")).all()]
        prompt = PROMISE_PROMPT.format(today=now.date().isoformat(), tracked=json.dumps(tracked, ensure_ascii=False) or "[]",
                                       mails="\n\n".join(untrusted("a mail the owner sent", m) for m in mails))
        answer = ctx.ask(prompt, JOB_RULES, purpose="promises")
        if not answer.ok:
            raise RuntimeError(f"model failed: {answer.error or answer.status}")
        out = extract_json(answer.text)
        if not isinstance(out, dict):
            raise ValueError("promise answer is not an object")
        valid_ids = {m["message_id"] for m in mails}
        times = good_times(self.schedule)
        new = 0
        for p in out.get("new") or []:
            if not isinstance(p, dict) or p.get("message_id") not in valid_ids or not str(p.get("what") or "").strip():
                continue
            by = None
            try:
                by = dt.datetime.combine(dt.date.fromisoformat(p["by"]), dt.time(18), tzinfo=tz) if p.get("by") else None
            except ValueError:
                pass
            _, created = upsert(s, kind="promise", source="promise", ref=f"{p['message_id']}:{clip(p['what'], 40)}",
                                what=str(p["what"]), who=clip(p.get("to_name"), 60) or None,
                                who_email=(str(p.get("to_email") or "").strip().lower() or None), due=by, since=now,
                                now=now, tz=tz, times=times)
            new += created
        closed = 0
        open_ids = {t["id"] for t in tracked}
        for lid in out.get("fulfilled") or []:
            if isinstance(lid, int) and lid in open_ids:
                close(s, lid, "done", now=now)
                closed += 1
        row.value = json.dumps(sorted(seen | valid_ids)[-300:])
        s.add(row)
        s.commit()
        return json.dumps({"mails": len(mails), "new": new, "fulfilled": closed})
