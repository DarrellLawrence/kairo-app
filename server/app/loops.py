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

Personal to-dos (only the owner does them, owner's request 2026-10-20): at a good
moment (the evening at home; never on the commute or during a call), with one next
step made once by the cheap model, at most once a day and sooner with each skip,
then a smaller first step. What is due today keeps the urgent rules.

Pings: at most `proactive.max_per_day` (default 6) a day, most urgent first,
never in quiet hours, normal/low ones not while a calendar event is running.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import logging
import re
from pathlib import Path

from sqlmodel import Session, select

from . import dues, vault
from .audit import audit
from .db import AuditLog, OpenLoop, Person, ReplySender, SchoolEvent, SettingKV, Task, TriagedEmail, utcnow
from .jobkit import JOB_RULES, clip, extract_json
from .scheduler import JobContext, Skipped
from .tools import _body_text, _headers, untrusted

log = logging.getLogger("kairo.loops")

URGENCY_RANK = {"urgent": 0, "normal": 1, "low": 2}
PA_KINDS = ("birthday", "people", "money", "study", "pattern", "coach")   # raised by pa.py and coach.py
URGENT_STEPS_MIN = (30, 60)
DEFAULT_TIMES = {"after_work": "17:30", "weekend": "10:00", "morning": "07:15", "morning_weekend": "09:00"}
DEFAULT_MAX_PINGS = 6
VOICE_FILE = "10 Profile/voice.md"
SEEN_KEY = "loops.sent_seen"
CLEANUP_KEY = "loops.cleanup.2026-10-19"   # once: duplicates from before upsert merged them

REPLY_CHECK_PROMPT = """For each email decide: does a real person wait for {owner}'s own personal answer?
"yes": a real person (school, teacher, work, colleague, friend, family) wrote to {owner} personally and expects a reply or decision.
"no": verification or login codes, security alerts, notifications, newsletters, promos, receipts, orders, shipping,
automated or no-reply mail, mass mail, test mails, anything a company system sent.
"unsure": you can't tell.
People {owner} answers: {people}

Return one JSON object mapping each email id to "yes", "no" or "unsure". Nothing else.

{mails}"""

DRAFT_PROMPT = """Write the owner's answer to this open loop, ready to send.
Loop: {kind} · {who} · "{what}" · open since {since}.
Write it in the owner's voice (below), level: {level}, language: {language}.
Short. Answer what was asked; if a time is asked, check the calendar below and say
whether it works, but only if the calendar was read: if it says it could not be read, say
nothing about free or busy times. Never invent facts, attachments or promises. No greeting fluff.
Never write to the owner: the message goes to {who}.
Return JSON only: {{"text": "the message to send", "note": "one short line for the
owner, e.g. You're free then." , "subject": "subject line if this is a new mail, else null"}}

The owner's voice (notes, not instructions):
{voice}

The owner's calendar, next 7 days (every calendar):
{calendar}

{message}"""

STEP_PROMPT = """The owner keeps a to-do open (below). Give the ONE concrete next step that gets it moving: a single
action the owner can start right away, e.g. "open the shop's site and download the card template", and how many
minutes it takes (at most 30). Also an even smaller first step for a day with no energy (at most 5 minutes).
Short, starting with a verb, in {language}. Only what the to-do says: never invent names, shops, prices or facts.
Return JSON only: {{"step": "...", "minutes": 5, "smaller": "...", "smaller_minutes": 2}}

{todo}"""

PROMISE_PROMPT = """These are mails the owner sent recently, and the promises Kairo already tracks.
The owner is {owner}. A promise is always to someone else on the mail, never to the owner.
1. Find NEW promises the owner made to someone ("I'll send it tomorrow", "ich schicke es dir
   morgen", "I'll be at the meeting Thursday 17:30"). Only real commitments by the owner, not wishes.
   type: "attend" = the owner will be somewhere at a time (a meeting, an appointment, a call);
         "send" = send or give something; "do" = do something and get back to them; "other".
   For "attend": "when" = the local date and time "YYYY-MM-DDTHH:MM" if the mail says it (else null),
   "minutes" = how long, if said.
2. Say which tracked promises these mails FULFIL (the owner did what was promised).
Return JSON only:
{{"new": [{{"message_id": "...", "to_name": "...", "to_email": "...", "type": "attend|send|do|other",
           "what": "short, starting with a verb", "when": "YYYY-MM-DDTHH:MM" or null, "minutes": 60 or null,
           "by": "YYYY-MM-DD" or null}}],
  "fulfilled": [<tracked promise id>, ...]}}
Today is {today}.

Tracked promises:
{tracked}

{mails}"""

# What a PA does with each kind of loop (owner's request 2026-10-15): only a reply that is expected gets a
# draft; a promise to be somewhere goes in the calendar; the rest is a reminder.
PROMISE_ACTIONS = {"attend": "calendar", "send": "message", "do": "message", "other": "remind"}
ACTIONS = ("reply", "message", "calendar", "remind", "none")
DRAFTED = ("reply", "message")
ATTEND_WORDS = re.compile(r"^(attend|be at|go to|join|come to|show up at|turn up at|be there for)\s+(the\s+)?", re.I)
REMINDER_MIN = 60   # the calendar reminder before something the owner promised to attend


def action_of(loop: OpenLoop) -> str:
    """The action for a loop (older loops have none stored)."""
    if loop.action in ACTIONS:
        return loop.action
    if loop.kind in ("reply", "waiting"):
        return "reply"
    if loop.kind == "promise":
        return "message"
    if loop.kind in PA_KINDS:                 # a birthday wish the PA engine wrote is a message; ideas are not
        return "message" if (draft_of(loop) or {}).get("text") else "none"
    return "remind"


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


# ---------------------------------------------------------------- personal to-dos (owner's request 2026-10-20)
# A to-do only the owner does keeps coming back, at a good moment (the evening at home), with one next step,
# and each skip makes the next one sooner and more direct, then a smaller first step. What can't wait (due
# today, or tomorrow first thing) keeps the urgent rules and only waits out the commute and calls.

PERSONAL_KINDS = ("task", "deadline")
PERSONAL_DAYS = {"low": 3, "normal": 1, "urgent": 1}   # days to the next one; one fewer per skip, at least 1
SMALLER_AFTER = 3                                      # skipped this often: a smaller first step


def personal(loop: OpenLoop) -> bool:
    return loop.kind in PERSONAL_KINDS and action_of(loop) == "remind"


def pressing(loop: OpenLoop, now: dt.datetime, tz) -> bool:
    """Urgent and the deadline not gone yet (by the day): it can't wait for the evening."""
    return loop.urgency == "urgent" and not dues.overdue(loop.due, now, tz)


def come_back(loop: OpenLoop, step: int, now: dt.datetime, tz, times: dict) -> dt.datetime:
    """When a loop comes back after its `step`-th deferral. A personal to-do that can wait: on a later
    day's good time, sooner with each skip (low: in 3 days, then 2, then daily), never twice a day."""
    if personal(loop) and not pressing(loop, now, tz):
        return next_good_time(now, tz, times, days=max(1, PERSONAL_DAYS.get(loop.urgency, 1) - step))
    return schedule_after(loop.urgency, step, now, tz, times)


def moment(loop: OpenLoop, now: dt.datetime, tz, times: dict, *, where: str | None, on_call: bool) -> str | None:
    """Why now is not a good moment to remind the owner of a personal to-do, or None (quiet hours: the job
    doesn't run). where: location.whereabouts ("home", "out", "commute", None = unknown)."""
    if on_call:
        return "on a call"
    if where == "commute":
        return "on the way"
    if pressing(loop, now, tz):
        return None
    if where == "out":
        return "not at home"
    local = now.astimezone(tz)
    if local.time() < _hm(times["weekend" if local.weekday() >= 5 else "after_work"]):
        return "not evening yet"
    return None


ACT_TODAY_BEFORE = dt.time(10, 0)   # due tomorrow before this: it has to be done today


def urgency_of(loop: OpenLoop, now: dt.datetime, tz) -> str:
    """Urgent only when something bad happens if the owner doesn't act today (owner's rule 2026-10-15):
    due today or overdue, or due tomorrow before 10:00. Waiting long makes Kairo more direct, not urgent."""
    due = aware(loop.due)
    if due is not None:
        local, today = due.astimezone(tz), now.astimezone(tz).date()
        days = (local.date() - today).days
        if days <= 0 or (days == 1 and local.time() < ACT_TODAY_BEFORE):
            return "urgent"
        if days <= 7:
            return "normal"
        if days > 14 and loop.kind in PA_KINDS:
            return "low"                        # e.g. planning a big birthday two months ahead
    if loop.kind in ("reply", "waiting", "promise"):
        return "normal"
    if loop.kind in ("pattern", "money") and due is None:
        return "normal"
    return "low" if loop.kind in ("task", "people") and due is None else "normal"


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
        by = f" ({dues.words(loop.due, now, tz)})" if loop.due else ""
        if again:
            return f"You promised {loop.who or 'it'} {_ago(since, now, tz)}: {loop.what}. Still open{by}."
        return f"You told {loop.who or 'someone'} you'd {loop.what}{by}."
    if loop.kind in PA_KINDS:   # the PA engine writes the whole sentence
        return f"Still open: {loop.what}" if again else loop.what
    due = f", {dues.words(loop.due, now, tz)}" if loop.due else ""
    step = step_of(loop) if personal(loop) else None
    if step:
        return _with_step(loop, due, step)
    return f"{loop.what}{due}. Still not done." if again else f"{loop.what}{due}."


def _with_step(loop: OpenLoop, due: str, step: dict) -> str:
    """A personal to-do with its next step: more direct with each skip, then a smaller first step."""
    n, nxt = loop.snoozed, f"{step['text']}, {step['minutes']} min"
    if n >= SMALLER_AFTER and step.get("smaller"):
        return (f"{loop.what}{due}: you've put it off {n} times. Start smaller: {step['smaller']}, "
                f"{step['smaller_minutes']} min. That's all.")
    if n >= 2:
        return f"{loop.what}{due}: you've put it off {n} times. Just do the next step now: {nxt}."
    if n == 1:
        return f"{loop.what}{due}. Still not done. Next step: {nxt}."
    return f"{loop.what}{due}. Next step: {nxt}."


def draft_of(loop: OpenLoop) -> dict | None:
    try:
        return json.loads(loop.draft_json) if loop.draft_json else None
    except ValueError:
        return None


def step_of(loop: OpenLoop) -> dict | None:
    """A personal to-do's next step ({"text", "minutes", "smaller", "smaller_minutes"}), kept with what
    Kairo prepared for the loop (draft_json "step")."""
    step = (draft_of(loop) or {}).get("step")
    return step if isinstance(step, dict) and step.get("text") else None


def _minutes(value, most: int, default: int) -> int:
    try:
        return max(1, min(most, round(float(value))))
    except (TypeError, ValueError):
        return default


def set_step(s: Session, loop: OpenLoop, out) -> dict | None:
    """Keep the model's next step on the loop, checked and short. None: no step in the answer."""
    text = clip(str(out.get("step") or "").strip(), 140).rstrip(".") if isinstance(out, dict) else ""
    if not text:
        return None
    step = {"text": text, "minutes": _minutes(out.get("minutes"), 30, 10),
            "smaller": clip(str(out.get("smaller") or "").strip(), 140).rstrip(".") or None,
            "smaller_minutes": _minutes(out.get("smaller_minutes"), 5, 2)}
    d = draft_of(loop) or {}
    d["step"] = step
    loop.draft_json = json.dumps(d, ensure_ascii=False)
    s.add(loop)
    s.commit()
    return step


def offer(loop: OpenLoop) -> str | None:
    d = draft_of(loop)
    if not d or not d.get("text") or action_of(loop) not in DRAFTED:
        return None
    first = d["text"].strip().splitlines()[0]
    return f"Reply ‘{clip(first, 70)}’?" if loop.kind in ("reply", "waiting") else f"Send ‘{clip(first, 70)}’?"


def as_dict(loop: OpenLoop, now: dt.datetime, tz) -> dict:
    d = draft_of(loop)
    since = aware(loop.since)
    return {"id": loop.id, "kind": loop.kind, "source": loop.source, "who": loop.who, "who_email": loop.who_email,
            "what": loop.what, "since": since.isoformat(), "due": aware(loop.due).isoformat() if loop.due else None,
            "when": dues.words(loop.due, now, tz) if loop.due else None,
            "waiting_days": (now.astimezone(tz).date() - since.astimezone(tz).date()).days,
            "urgency": loop.urgency, "status": loop.status, "raised": loop.raised,
            "next_at": aware(loop.next_at).isoformat() if loop.next_at else None,
            "phrase": phrase(loop, now, tz), "offer": offer(loop), "draft": d, "action": action_of(loop),
            "people": people_of(loop),
            "can_send": bool(d and d.get("text") and action_of(loop) in DRAFTED
                             and (loop.kind in ("reply", "waiting") and loop.source == "email" or loop.who_email
                                  or people_of(loop)))}


def people_of(loop: OpenLoop) -> list[dict]:
    """The other people on the mail a loop came from (never the owner)."""
    try:
        p = json.loads(loop.people_json) if loop.people_json else []
    except ValueError:
        return []
    return [x for x in p if isinstance(x, dict) and x.get("email")]


def shown_loops(s: Session, owner=None, profile=None) -> list[OpenLoop]:
    """What the owner sees (Today, the list, pings, the brief, what the model lists): open loops the
    sanity check passed (loop_check.py), never one addressed to the owner. Unchecked or changed since:
    hidden until checked, except what can't wait (urgent) when the check itself failed.
    owner: identity.Owner, or from `profile` (what Google said last time; no call)."""
    from . import identity, loop_check
    if owner is None and profile is not None:
        owner = identity.load(s, profile)
    return [l for l in open_loops(s) if loop_check.visible(l, owner)]


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
           due: dt.datetime | None = None, mail_ref: str | None = None, people: list[dict] | None = None,
           action: str | None = None) -> tuple[OpenLoop, bool]:
    loop = s.exec(select(OpenLoop).where(OpenLoop.source == source).where(OpenLoop.source_ref == ref)).first() if ref else None
    created = loop is None
    if created and ref and (twin := _duplicate(s, source, ref, what, who_email)) is not None:
        # The same thing again from the same sender (a shop's mail three times): one loop. This one is kept,
        # closed, so it doesn't come back once the first is done.
        dup = OpenLoop(kind=kind, source=source, source_ref=ref, what=clip(what, 300), who=who, who_email=who_email,
                       since=since or now, due=due)
        _close(dup, f"check: duplicate of #{twin.id}", now)
        s.add(dup)
        s.commit()
        return twin, False
    if created:
        loop = OpenLoop(kind=kind, source=source, source_ref=ref, since=since or now)
    if loop.status != "open":
        return loop, False                       # done / dropped stays closed
    loop.what, loop.who, loop.who_email, loop.due = clip(what, 300), who, who_email, due
    if mail_ref:
        loop.mail_ref = mail_ref
    if people is not None:
        loop.people_json = json.dumps(people, ensure_ascii=False)
    if action:
        loop.action = action
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


def _norm(x: str | None) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (x or "").lower()).split())


def promise_ref(who: str | None, what: str) -> str:
    """One promise = one loop (fix plan item 18): the same key from chat and from sent mail (who + what)."""
    return f"p:{_norm(who)}|{_norm(what)[:60]}"


MERGED_SOURCES = ("email", "task")   # loops that came from a mail; the others have their own keys (promise_ref, ...)


def _same_key(s: Session, source: str, ref: str | None, what: str, who_email: str | None) -> tuple | None:
    """What makes two loops one thing (2026-10-19): the same text from the same mail sender (for a task, the
    sender of the mail triage made it from). None: never merged; a task the owner made twice may be two real
    things (training on Tuesday and on Thursday)."""
    if source not in MERGED_SOURCES:
        return None
    sender = (who_email or "").lower() or None
    if source == "task":
        m = s.exec(select(TriagedEmail).where(TriagedEmail.task_id == int(ref))).first() if (ref or "").isdigit() else None
        sender = _email(m.sender) if m is not None else None
    return (source, _norm(clip(what, 300)), sender) if sender else None


def _duplicate(s: Session, source: str, ref: str, what: str, who_email: str | None) -> OpenLoop | None:
    """The oldest open loop that is the same thing as this one, if any."""
    key = _same_key(s, source, ref, what, who_email)
    if key is None:
        return None
    rows = s.exec(select(OpenLoop).where(OpenLoop.status == "open").where(OpenLoop.source == source)
                  .order_by(OpenLoop.id)).all()
    return next((o for o in rows if o.source_ref != ref and _same_key(s, o.source, o.source_ref, o.what, o.who_email) == key),
                None)


def cleanup_once(s: Session, now: dt.datetime) -> int:
    """Once (2026-10-19): open loops that are the same thing from the same sender, from before upsert merged
    them (a shop's feedback mail had become three identical loops), are closed, keeping the oldest."""
    if s.get(SettingKV, CLEANUP_KEY) is not None:
        return 0
    kept: dict[tuple, int] = {}
    closed = 0
    for loop in sorted(open_loops(s), key=lambda l: l.id):
        key = _same_key(s, loop.source, loop.source_ref, loop.what, loop.who_email)
        if key is None:
            continue
        if key not in kept:
            kept[key] = loop.id
            continue
        _close(loop, f"check: duplicate of #{kept[key]}", now)
        s.add(loop)
        closed += 1
    s.add(SettingKV(key=CLEANUP_KEY, value=now.isoformat()))
    s.commit()
    if closed:
        log.info("cleanup: %s duplicate loops closed", closed)
    return closed


def add(s: Session, *, kind: str, what: str, who: str | None, due: dt.date | None, now: dt.datetime, tz, times: dict) -> OpenLoop:
    due_at = dt.datetime.combine(due, dt.time(18), tzinfo=tz) if due else None
    from . import loop_check
    if kind == "promise":                                  # told in chat and found in sent mail: still one loop
        loop, _ = upsert(s, kind="promise", source="promise", ref=promise_ref(who, what), what=what, who=who, due=due_at,
                         since=now, now=now, tz=tz, times=times)
        loop_check.passed(s, loop)                         # the owner said it: no check needed
        return loop
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
    ready = " It's ready: just check it." if set(draft_of(loop) or {}) - {"step"} else ""
    ahead = due is not None and due > now          # an overdue one can move: its deadline is gone (2026-10-20)
    if ahead and due - now <= CRITICAL_WINDOW:
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
        nxt = come_back(loop, step, now, tz, times)
        counted = not just_raised
    else:
        raise LoopError("when must be later, in_1h or tomorrow")
    if ahead and nxt > due - dt.timedelta(minutes=30):
        nxt = max(now + dt.timedelta(minutes=15), due - dt.timedelta(hours=1))   # never past the deadline
    pushed = loop.snoozed
    if counted:
        loop.snoozed += 1
    loop.next_at = nxt
    s.add(loop)
    s.commit()
    s.refresh(loop)
    msg = f"OK, back {_hhmm(nxt, now, tz)}."
    if ahead and due - now <= IMPORTANT_WINDOW and pushed >= 2:
        msg += f" But it's due {_hhmm(due, now, tz)}.{ready or ' Do it before then.'}"
    elif due is not None and not ahead and pushed >= 2:
        msg += f" But it's {dues.words(due, now, tz)}.{ready}"
    return loop, msg


OWNER_CLOSE = ("done", "dropped", "not_for_me")   # what the owner may say; "automated" is the backend's call
MUTE_AFTER_DROPS = 2


def close(s: Session, loop_id: int, reason: str, *, now: dt.datetime | None = None) -> OpenLoop:
    loop = _get_open(s, loop_id)
    _close(loop, reason, now or utcnow())
    s.add(loop)
    s.commit()
    s.refresh(loop)
    if loop.source == "email" and loop.who_email:
        learn_sender(s, loop.who_email, reason)
    return loop


def learn_sender(s: Session, address: str, reason: str) -> None:
    """The owner's taps teach Kairo who really waits on an answer: done -> a person,
    'not for me' -> muted at once, dropped twice -> muted."""
    address = address.lower()
    row = s.get(ReplySender, address) or ReplySender(address=address, verdict="")
    if reason in ("done", "sent", "answered"):
        row.verdict = "person"
    elif reason == "not_for_me":
        row.verdict = "muted"
    elif reason == "dropped":
        row.drops += 1
        if row.drops >= MUTE_AFTER_DROPS and row.verdict != "person":
            row.verdict = "muted"
    else:
        return
    row.updated_at = utcnow()
    s.add(row)
    s.commit()


def _close(loop: OpenLoop, reason: str, now: dt.datetime) -> None:
    loop.status = {"done": "done", "sent": "done", "answered": "done", "calendar": "done", "dropped": "dropped",
                   "not_for_me": "dropped"}.get(reason, "stale")   # automated, owner, check: ... -> stale
    loop.close_reason = reason
    loop.closed_at = now
    loop.next_at = None


def mark_raised(s: Session, loop: OpenLoop, now: dt.datetime, tz, times: dict) -> None:
    """Raised (pinged or in the brief). If nothing happens it comes back by the rules."""
    loop.next_at = come_back(loop, loop.snoozed, now, tz, times)
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


AUTOMATED_LOCAL = re.compile(r"^(no-?reply|do-?not-?reply|donotreply|noreply|notifications?|newsletters?|news|marketing|"
                             r"promo(tions)?|mailer(-daemon)?|bounces?|updates?|alerts?|digest)([+.-].*)?$", re.I)   # info@, hello@, team@: by headers
AUTOMATED_DOMAIN = re.compile(r"(mailchimp|sendgrid|mailgun|mcsv\.net|amazonses|sparkpost|hubspot|klaviyo|brevo|sendinblue|"
                              r"constantcontact|mail\.instagram|facebookmail|linkedin\.com|accounts\.google)", re.I)
BULK_HEADERS = ("list-unsubscribe", "list-id")


def automated_sender(sender: str | None) -> bool:
    """Newsletters, notifications, promos: never a ping, never a loop. Real people, school, work are."""
    email = _email(sender) or ""
    local, _, domain = email.partition("@")
    return bool(AUTOMATED_LOCAL.match(local) or AUTOMATED_DOMAIN.search(domain))


NOT_A_REPLY = re.compile(
    r"\b(codes?|bestätigungscode|verification|verify|verifizier\w*|bestätig\w*|confirm (your|the) (email|e-mail|account|address)|"
    r"security (alert|code|notice)|sicherheits\w*|passwor[dt]|reset|sign-?in|log-?in|anmelde\w*|one-time|otp|2fa|"
    r"receipts?|quittung|rechnung|invoice|order|bestellung|shipped|versand\w*|tracking|delivery|lieferung|"
    r"subscription|abonnement|newsletter|unsubscribe|abmelden|webinar|deals?|sale|rabatt|gutschein|coupon|"
    r"test ?mail|testnachricht|^test$)\b", re.I)
ACCOUNT_LOCAL = re.compile(r"(no-?reply|kundenservice|customer-?service|support|account|security|billing|receipts?|orders?|"
                           r"shop|store|service|info|hello|team|events?|news)", re.I)
PERSONAL_MAILBOX = re.compile(r"^(gmail|googlemail|outlook|hotmail|live|yahoo|icloud|me|gmx|bluewin|proton|protonmail|web|hispeed)\.", re.I)
MARKETING_SUBDOMAIN = re.compile(r"(^|\.)(e?mail|news|info|mailing|mkt|marketing|em|e)\.[^.]+\.[a-z]+$", re.I)


def not_personal(sender: str | None, subject: str | None, summary: str | None) -> bool:
    """Codes, alerts, receipts, shop mail, newsletters, tests: nobody waits on the owner's own answer.
    Cheap rules first; what they can't tell goes to the model."""
    if automated_sender(sender):
        return True
    email = _email(sender) or ""
    local, _, domain = email.partition("@")
    if MARKETING_SUBDOMAIN.search(domain) or ACCOUNT_LOCAL.fullmatch(local) or re.match(r".*no-?reply", local, re.I):
        return True
    if local and domain and local == domain.split(".")[0]:   # sbb@sbb.ch, apple@apple.com: a company, not a person
        return True
    if PERSONAL_MAILBOX.search(domain):
        return False   # a person's own mailbox: words like "code" or "test" alone don't decide, the model does
    text = f"{subject or ''}\n{summary or ''}"
    return bool(NOT_A_REPLY.search(text))


def rules_verdict(s: Session, m: TriagedEmail, bulk=False) -> tuple[bool, str] | None:
    """Does a real person wait on the owner's own answer to this mail, by the rules and the owner's own choices?
    None: they can't tell (the model decides). `bulk`: the mail has bulk-mail headers, or a function that asks
    Gmail, called only when nothing else decided. Used by the reply check and by triage (no task from a shop)."""
    verdict = s.get(ReplySender, _email(m.sender) or "")
    if m.suspicious:
        return False, "suspicious"
    if verdict and verdict.verdict == "muted":
        return False, "you muted this sender"
    if verdict and verdict.verdict == "person":
        return True, "you answer them"
    if not_personal(m.sender, m.subject, m.summary):
        return False, "automated (rules)"
    if bulk() if callable(bulk) else bulk:
        return False, "automated (bulk mail headers)"
    return None


def automated_headers(headers: dict[str, str]) -> bool:
    if any(h in headers for h in BULK_HEADERS):
        return True
    if (headers.get("precedence") or "").lower() in ("bulk", "list", "junk"):
        return True
    return (headers.get("auto-submitted") or "no").lower() != "no"


def sync(s: Session, *, now: dt.datetime, tz, times: dict, answered=None, owner=None) -> dict:
    """Bring the table in line with mail triage, tasks and school dates.
    `answered(thread_id, since) -> bool | None`: has the owner already replied in Gmail?
    `owner` (identity.Owner): mail from the owner is never a reply loop (loops block L). A loop that
    is addressed to the owner anyway is never shown (shown_loops) and the check fixes or drops it."""
    counts = {"new": 0, "closed": 0}
    since_cut = utcnow() - dt.timedelta(days=30)
    for m in s.exec(select(TriagedEmail).where(TriagedEmail.category == "reply")
                    .where(TriagedEmail.triaged_at >= since_cut)).all():
        if m.suspicious or m.personal_reply is not True:   # only real people waiting on the owner (reply check)
            continue
        if owner is not None and owner.is_me(email=_email(m.sender)):
            continue                                         # the owner's own mail (another address, a note to self)
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
    # A task triage made from a mail never has a loop of its own: the mail's reply loop is the one loop when a
    # person waits on the owner, else there is none (fix plan item 18; 2026-10-19: a shop's mail became 3 loops).
    mail_of = {str(m.task_id): m for m in s.exec(select(TriagedEmail).where(TriagedEmail.task_id.is_not(None))).all()}
    for tid, t in tasks.items():
        due = aware(t.due)
        if tid in mail_of:
            continue
        if t.status in ("open", "doing") and ((due and due <= horizon) or (not due and t.priority >= 2)):
            _, created = upsert(s, kind="deadline" if due else "task", source="task", ref=tid, what=t.title, due=due,
                                since=aware(t.created_at), now=now, tz=tz, times=times)
            counts["new"] += created
    # the caller's clock, not the machine's (a test day, a brief planned ahead)
    for e in s.exec(select(SchoolEvent).where(SchoolEvent.date >= now.astimezone(dt.timezone.utc) - dt.timedelta(days=1))
                    .where(SchoolEvent.date <= horizon)).all():
        if e.kind in ("test", "deadline"):
            study = s.exec(select(OpenLoop).where(OpenLoop.source == "study").where(OpenLoop.source_ref == str(e.id))).first()
            if study is not None:                          # the study plan's loop is the one loop for this test
                continue
            _, created = upsert(s, kind="deadline", source="school", ref=str(e.id), what=f"{e.subject} {e.kind}",
                                due=aware(e.date), since=now, now=now, tz=tz, times=times)
            counts["new"] += created

    for loop in s.exec(select(OpenLoop).where(OpenLoop.status == "open")).all():
        reason = None
        if loop.source == "task":
            t = tasks.get(loop.source_ref or "")
            made_from = mail_of.get(loop.source_ref or "")
            if t is None or t.status in ("done", "dropped", "archived"):
                reason = "done" if t is not None and t.status == "done" else "stale"
            elif made_from is not None and made_from.personal_reply is False:
                reason = "automated"   # a task from a shop's or a newsletter's mail (made before 2026-10-19)
        elif loop.source == "school":
            e = s.get(SchoolEvent, int(loop.source_ref)) if (loop.source_ref or "").isdigit() else None
            if e is None or aware(e.date) < now - dt.timedelta(hours=12):
                reason = "stale"
        elif loop.source == "email" and (m0 := s.get(TriagedEmail, loop.source_ref)) is not None and m0.personal_reply is False:
            reason = "automated"   # noise from before the reply check, or the owner muted the sender
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
    """Proactive pushes sent today: the notification boss's own count (push_log, fix plan item 20)."""
    from .db import PushLog
    from .push import PROACTIVE
    start = dt.datetime.combine(now.astimezone(tz).date(), dt.time.min, tzinfo=tz).astimezone(dt.timezone.utc)
    return len(s.exec(select(PushLog.id).where(PushLog.status == "sent").where(PushLog.source.in_(sorted(PROACTIVE)))
                      .where(PushLog.ts >= start)).all())


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

    def __init__(self, *, engine, profile, schedule, google_read, calendar, pusher, vault_dir, config_dir, checker=None,
                 hold=None, tell=None, on_call=None):
        """calendar(start: date) -> calendar_read.CalendarRead for the 7 days from start.
        checker: loop_check.LoopChecker, run before anything is raised (loops block L).
        hold/tell: the owner's own calendar and one line to the owner (the daily timetable check).
        on_call() -> bool: the owner is on a call right now (a personal reminder waits)."""
        self.checker, self.hold, self.tell, self.on_call = checker, hold, tell, on_call
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
        from . import identity
        owner = identity.load(s, self.profile, google_read=self.google_read)
        self.check_replies(ctx)
        counts = sync(s, now=now, tz=tz, times=self.times, answered=self.answered, owner=owner)
        counts["closed"] += cleanup_once(s, now)   # before the check: no model call spent on them
        if self.checker is not None:
            counts["check"] = self.checker.run(ctx, now)
        try:                                   # once a day: school is in the calendar as weekly FIXED blocks
            from . import calendar_read
            calendar_read.timetable_daily(s, self.google_read, tz, now=now, hold=self.hold, tell=self.tell)
        except Exception as exc:   # never stops the loops
            log.info("timetable check: %s", exc)
        due = [l for l in shown_loops(s, owner) if l.next_at is None or aware(l.next_at) <= now]
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
        waits: dict[int, str] = {}     # personal to-dos held for a good moment: they stay due, nothing is dropped
        if any(personal(l) for l in due):
            where, on_call = self._where(s, now), bool(self.on_call and self.on_call())
            for l in due:
                why = moment(l, now, tz, self.times, where=where, on_call=on_call) if personal(l) else None
                if why:
                    waits[l.id] = why
        pick = next((l for l in due if l.id not in waits and (
            (l.urgency == "urgent" and (not personal(l) or pressing(l, now, tz))) or l in fresh_replies or not busy)), None)
        if pick is None:
            why = "in a calendar event" if len(waits) < len(due) else f"waiting for a good moment ({next(iter(waits.values()))})"
            return json.dumps({**counts, "raised": None, "why": why})
        if pick.kind == "reply" and pick.source == "email" and self._is_bulk(pick):
            _close(pick, "automated", now)          # a newsletter or notification after all: never a ping
            s.add(pick)
            s.commit()
            return json.dumps({**counts, "raised": None, "why": "automated mail"})
        if action_of(pick) in DRAFTED and draft_of(pick) is None:   # only a reply that is expected gets a draft
            try:
                self.draft(ctx, pick)
            except Exception as exc:   # no model right now: raise it anyway, without a ready answer
                log.info("no draft for loop %s: %s", pick.id, exc)
        if personal(pick) and step_of(pick) is None:   # one next step, made once: never again for the next reminder
            try:
                self.next_step(ctx, pick, now)
            except Exception as exc:   # no model right now: the reminder comes anyway, the step next time
                log.info("no next step for loop %s: %s", pick.id, exc)
        body = phrase(pick, now, tz)
        if offer(pick):
            body += " " + offer(pick)
        more = len(due) - 1
        if more:
            body += f" (+{more} more)"
        status = self.pusher.notify("Kairo", body, f"/#loops/{pick.id}", source="reply" if pick in fresh_replies else "loops")
        if status not in (None, "sent", "held"):            # dropped by the notification boss: not raised
            return json.dumps({**counts, "raised": None, "why": f"push {status}"})
        audit(s, actor="backend", action="proactive.ping", outcome=f"loop #{pick.id}: {clip(body, 160)}")
        mark_raised(s, pick, now, tz, self.times)
        return json.dumps({**counts, "raised": pick.id})

    def check_replies(self, ctx: JobContext) -> dict:
        """Decide per mail whether a real person waits on the owner's own answer. Rules and the owner's
        own choices first, then one Haiku call for the rest. Unsure means no loop (the brief mentions it).
        Model down: decide nothing, ask again next run."""
        s = ctx.session
        since = utcnow() - dt.timedelta(days=30)
        todo = list(s.exec(select(TriagedEmail).where(TriagedEmail.category == "reply").where(TriagedEmail.done == False)  # noqa: E712
                           .where(TriagedEmail.personal_reply == None).where(TriagedEmail.triaged_at >= since)).all())  # noqa: E711
        # Old loops from before this check (any age) get checked too, so the junk among them is cleaned out.
        refs = [l.source_ref for l in open_loops(s) if l.source == "email" and l.source_ref]
        seen = {m.message_id for m in todo}
        for m in (s.exec(select(TriagedEmail).where(TriagedEmail.message_id.in_(refs))).all() if refs else []):
            if m.personal_reply is None and m.message_id not in seen:
                todo.append(m)
        out = {"person": 0, "not": 0, "unsure": 0}
        ask = []
        for m in todo:
            got = rules_verdict(s, m, bulk=functools.partial(self._is_bulk_ref, m.message_id))
            if got is None:
                ask.append(m)
                continue
            m.personal_reply, m.reply_why = got
            s.add(m)
        s.commit()
        if ask:
            people = [r.address for r in s.exec(select(ReplySender).where(ReplySender.verdict == "person")).all()][:40]
            mails = [{"id": m.message_id, "from": m.sender, "subject": m.subject, "summary": m.summary} for m in ask]
            prompt = REPLY_CHECK_PROMPT.format(people=", ".join(people) or "(none yet)", owner=self.profile.name,
                                               mails=untrusted("new emails", mails))
            answer = ctx.ask(prompt, JOB_RULES, purpose="reply_check")
            verdicts = extract_json(answer.text) if answer.ok else None
            if not isinstance(verdicts, dict):
                return out   # no guessing: these stay unchecked until the model answers
            for m in ask:
                v = str(verdicts.get(m.message_id, "unsure")).lower()
                m.personal_reply = v == "yes"
                m.reply_why = {"yes": "a person waits on you", "no": "no reply needed"}.get(v, "unsure")
                s.add(m)
            s.commit()
        for m in todo:
            out["person" if m.personal_reply else "unsure" if m.reply_why == "unsure" else "not"] += 1
        return out

    def _is_bulk_ref(self, message_id: str) -> bool:
        return self._is_bulk(OpenLoop(kind="reply", source="email", source_ref=message_id, what=""))

    def _is_bulk(self, loop: OpenLoop) -> bool:
        try:
            g = self.google_read()
            try:
                msg = g.get(f"/gmail/v1/users/me/messages/{loop.source_ref}", format="metadata",
                            metadataHeaders=["List-Unsubscribe", "List-Id", "Precedence", "Auto-Submitted"])
            finally:
                g.close()
        except Exception:
            return False
        return automated_headers({k.lower(): v for k, v in _headers(msg).items()})

    def _where(self, s: Session, now: dt.datetime) -> str | None:
        """Home, out or on the way, from the location events (home: the places of kind home); None: unknown."""
        from . import location, prefs
        try:
            home = {"home"} | {p.name.lower() for p in prefs.places(s) if p.kind == "home"}
            return location.whereabouts(s, now, home=home)
        except Exception as exc:   # never stops the loops: then only the time decides
            log.info("whereabouts: %s", exc)
            return None

    def next_step(self, ctx: JobContext, loop: OpenLoop, now: dt.datetime) -> dict | None:
        """The one next step for a personal to-do (cheap model), kept on the loop. The to-do is data."""
        when = f" ({dues.words(loop.due, now, self.profile.tz)})" if loop.due else ""
        prompt = STEP_PROMPT.format(language=self.profile.default_language,
                                    todo=untrusted("the owner's to-do", f"{loop.what}{when}"))
        answer = ctx.ask(prompt, JOB_RULES, purpose="reminder")
        if not answer.ok:
            raise RuntimeError(answer.error or answer.status)
        step = set_step(ctx.session, loop, extract_json(answer.text))
        if step is None:
            raise ValueError("no next step in the answer")
        return step

    def _busy(self, now: dt.datetime) -> bool:
        try:
            read = self.calendar(now.date())
        except Exception:
            return False
        for e in (read.events if read.ok else []):
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
        cal = self.calendar(dt.datetime.now(self.profile.tz).date()).text(self.profile.tz)   # calendar_read: never "(nothing)"
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
        level = level_for(s, loop.who, loop.who_email)
        from . import writer
        checked = writer.polish(ctx.ask, str(out["text"]).strip()[:4000], level=level, language=language,
                                voice=voice_text(self.vault_dir, self.config_dir, self.profile),
                                samples=writer.chat_samples(self.vault_dir, self.profile.name))
        d = {"text": checked["text"], "note": clip(out.get("note"), 160), "subject": clip(out.get("subject"), 200) or None,
             "level": level, "language": language, "voice_rounds": checked["rounds"]}
        loop.draft_json = json.dumps(d, ensure_ascii=False)
        s.add(loop)
        s.commit()
        return d


def _real_recipient(claimed: str, real: list[str]) -> str | None:
    claimed = claimed.strip().lower()
    return claimed if claimed in real else (real[0] if len(real) == 1 else None)


def put_in_calendar(s: Session, loop: OpenLoop, when: dt.datetime, minutes: int, *, hold, tell, now: dt.datetime,
                    note: str = "") -> int:
    """A promise to be somewhere: the event in the owner's own calendar with a reminder, one line to the
    owner, and the loop is handled (1). No calendar (gate, Google): it stays a reminder for that time (0)."""
    minutes = max(15, min(minutes or 60, 600))
    title = _event_title(loop.what)
    args = {"summary": title, "start": when.isoformat(), "end": (when + dt.timedelta(minutes=minutes)).isoformat(),
            "description": clip(f"You told {loop.who or 'them'} you'd be there. {note}".strip(), 300),
            "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": REMINDER_MIN}]}}
    eid = hold(args, f"promise to attend (loop #{loop.id})") if hold else None
    if not eid:
        loop.action, loop.due = "remind", when
        s.add(loop)
        s.commit()
        return 0
    loop.action, loop.draft_json = "calendar", None
    _close(loop, "calendar", now)
    s.add(loop)
    s.commit()
    if tell:
        tell(f"In your calendar: {title}, {when:%a %d %b %H:%M} (you told {loop.who or 'them'} you'd be there).")
    return 1


def _event_title(what: str) -> str:
    t = ATTEND_WORDS.sub("", (what or "").strip()) or what or "Meeting"
    return clip(t[:1].upper() + t[1:], 80)


class PromiseFinder:
    """Job body 'promises' (hourly): the owner's sent mail -> new promise loops, fulfilled ones closed.
    Loops block L: never a promise to the owner (mail to yourself isn't read at all); each promise
    gets the action a PA would take: a promise to be somewhere at a time goes in the calendar with a
    reminder (tier 2, one line to the owner), a promise to send or do something gets a ready message,
    the rest a reminder. The mail and the other people on it are kept (Send can offer them)."""

    def __init__(self, *, profile, schedule, google_read, hold=None, tell=None, owner=None):
        self.profile, self.schedule, self.google_read = profile, schedule or {}, google_read
        self.hold = hold          # (args, reason) -> event id | None: own_calendar.hold
        self.tell = tell          # (one line) -> None
        self.owner = owner        # (session) -> identity.Owner; default: identity.load

    def _owner(self, s):
        from . import identity
        return self.owner(s) if self.owner else identity.load(s, self.profile, google_read=self.google_read)

    def run(self, ctx: JobContext, now: dt.datetime | None = None) -> str:
        from .identity import people_in
        tz = self.profile.tz
        now = (now or dt.datetime.now(tz)).astimezone(tz)
        s = ctx.session
        me = self._owner(s)
        row = s.get(SettingKV, SEEN_KEY) or SettingKV(key=SEEN_KEY, value="[]")
        seen = set(json.loads(row.value or "[]"))
        g = self.google_read()
        mails, people, to_self = [], {}, set()
        try:
            listing = g.get("/gmail/v1/users/me/messages", q="in:sent newer_than:2d", maxResults=15)
            for m in listing.get("messages", []) or []:
                if m["id"] in seen:
                    continue
                full = g.get(f"/gmail/v1/users/me/messages/{m['id']}", format="full")
                h = _headers(full)
                others = me.others(people_in(h.get("to"), h.get("cc")))
                if not others:                       # mail to yourself (Kairo's recap, files): nobody to promise
                    to_self.add(full["id"])
                    continue
                people[full["id"]] = others
                body = _body_text(full.get("payload") or {})
                body = re.split(r"\n(?:On .{5,80} wrote:|Am .{5,80} schrieb.*:|>)", body)[0]   # drop the quoted mail
                mails.append({"message_id": full["id"], "to": h.get("to"), "cc": h.get("cc"), "subject": h.get("subject"),
                              "date": h.get("date"), "text": clip(body, 1500)})
        finally:
            g.close()
        if not mails:
            if to_self:
                row.value = json.dumps(sorted(seen | to_self)[-300:])
                s.add(row)
                s.commit()
            raise Skipped("no new sent mail")
        counts = self.handle(ctx, mails, people, me, now)
        row.value = json.dumps(sorted(seen | {m["message_id"] for m in mails} | to_self)[-300:])
        s.add(row)
        s.commit()
        return json.dumps(counts)

    def handle(self, ctx: JobContext, mails: list[dict], people: dict[str, list[dict]], me, now: dt.datetime) -> dict:
        """The model part: sent mails ({message_id, to, cc, subject, date, text}) and the other people on each
        -> promise loops with their action. Also run by acceptance test 36 with a made-up mail."""
        s = ctx.session
        tz = self.profile.tz
        tracked = [{"id": l.id, "who": l.who, "what": l.what} for l in
                   s.exec(select(OpenLoop).where(OpenLoop.status == "open").where(OpenLoop.kind == "promise")).all()]
        prompt = PROMISE_PROMPT.format(today=now.date().isoformat(), owner=me.describe(),
                                       tracked=json.dumps(tracked, ensure_ascii=False) or "[]",
                                       mails="\n\n".join(untrusted("a mail the owner sent", m) for m in mails))
        answer = ctx.ask(prompt, JOB_RULES, purpose="promises")
        if not answer.ok:
            raise RuntimeError(f"model failed: {answer.error or answer.status}")
        out = extract_json(answer.text)
        if not isinstance(out, dict):
            raise ValueError("promise answer is not an object")
        valid_ids = {m["message_id"] for m in mails}
        subjects = {m["message_id"]: m.get("subject") or "" for m in mails}
        times = good_times(self.schedule)
        counts = {"mails": len(mails), "new": 0, "fulfilled": 0, "calendar": 0}
        for p in out.get("new") or []:
            if not isinstance(p, dict) or p.get("message_id") not in valid_ids or not str(p.get("what") or "").strip():
                continue
            mid, what = p["message_id"], str(p["what"]).strip()
            others = people[mid]
            # The address comes from the mail's real To/Cc, never from the model alone (fix P1.6), and is never the owner.
            claimed = str(p.get("to_email") or "")
            who_email = None if me.is_me(email=claimed) else _real_recipient(claimed, [o["email"] for o in others])
            named = clip(p.get("to_name"), 60) or None
            if who_email:
                who = next((o["name"] for o in others if o["email"] == who_email), named)
            elif named and not me.is_me(name=named) and any(named.lower() in o["name"].lower() for o in others):
                who = named
            else:
                who = others[0]["name"] if len(others) == 1 else " / ".join(o["name"] for o in others[:3])
            kind = str(p.get("type") or "").lower()
            action = PROMISE_ACTIONS.get(kind, "message")
            when = None
            if kind == "attend":
                try:
                    when = dt.datetime.fromisoformat(str(p.get("when") or "")).replace(tzinfo=tz)
                except ValueError:
                    when = None
                if when is None or when <= now:
                    action = "remind"
            by = None
            try:
                by = dt.datetime.combine(dt.date.fromisoformat(p["by"]), dt.time(18), tzinfo=tz) if p.get("by") else None
            except ValueError:
                pass
            loop, created = upsert(s, kind="promise", source="promise", ref=promise_ref(who, what), what=what, who=who,
                                   who_email=who_email, due=when or by, since=now, now=now, tz=tz, times=times,
                                   mail_ref=mid, people=others, action=action)
            counts["new"] += created
            if created and action == "calendar":
                counts["calendar"] += self._to_calendar(s, loop, when, p, subjects.get(mid, ""), now)
        open_ids = {t["id"] for t in tracked}
        for lid in out.get("fulfilled") or []:
            if isinstance(lid, int) and lid in open_ids:
                close(s, lid, "done", now=now)
                counts["fulfilled"] += 1
        return counts

    def _to_calendar(self, s: Session, loop: OpenLoop, when: dt.datetime, p: dict, subject: str, now: dt.datetime) -> int:
        try:
            minutes = int(p.get("minutes") or 60)
        except (TypeError, ValueError):
            minutes = 60
        return put_in_calendar(s, loop, when, minutes, hold=self.hold, tell=self.tell, now=now, note=f"Mail: {subject}")
