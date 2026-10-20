"""Evening recap (v7 item 10): a short mail to the owner's own address, label Kairo/Recap.

Built by code from Kairo's own data (no model run): done today, still open, tomorrow,
new on OLAT. No notes about other people (item 14): people nudges and gift ideas
stay in the app and the vault. Sent through the gate as gmail.send_self (tier 2).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re

from sqlmodel import Session, select

from . import dues
from .db import OpenLoop, PortalItem, SettingKV, Task
from .jobkit import clip

log = logging.getLogger("kairo.recap")

LABEL = "Kairo/Recap"
SENT_KEY = "recap.last_sent"
PRIVATE_KINDS = ("people", "birthday", "money", "pattern")   # stay in the app (item 14, money)


def _aware(t):
    return t if t is None or t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _plain(text: str) -> str:
    return re.sub(r"\s*[—–]\s*", ", ", text or "").strip()


class Recap:
    def __init__(self, *, engine, gate, executor, tz, calendar):
        self.engine, self.gate, self._executor, self.tz, self.calendar = engine, gate, executor, tz, calendar

    def build(self, s: Session, now: dt.datetime) -> tuple[str, str]:
        local = now.astimezone(self.tz)
        start = dt.datetime.combine(local.date(), dt.time(0), tzinfo=self.tz)
        done = [t.title for t in s.exec(select(Task).where(Task.status == "done")).all()
                if t.done_at and _aware(t.done_at) >= start]
        done += [l.what for l in s.exec(select(OpenLoop).where(OpenLoop.status == "done")).all()
                 if l.closed_at and _aware(l.closed_at) >= start and l.kind not in PRIVATE_KINDS]
        open_loops = [l.what for l in s.exec(select(OpenLoop).where(OpenLoop.status == "open")).all()
                      if l.kind not in PRIVATE_KINDS][:4]
        overdue = [f"{t.title} ({dues.words(t.due, now, self.tz)})"   # by the day: a date-only due today isn't late yet
                   for t in s.exec(select(Task).where(Task.status.in_(["open", "doing"]))).all()
                   if dues.overdue(t.due, now, self.tz)][:3]
        try:
            events, _ = self.calendar(local.date() + dt.timedelta(days=1))
        except Exception as exc:   # calendar down: the recap still goes out
            log.info("recap without tomorrow's calendar: %s", exc)
            events = []
        tomorrow = []
        for e in sorted((e for e in events or [] if not e.get("all_day") and e.get("start")), key=lambda e: e["start"])[:4]:
            title = re.sub(r"^\[Kairo\]\s*", "", e.get("summary") or "")
            tomorrow.append(f"{e['start'][11:16]} {title}")
        olat = [f"{i.course}: {i.title}" for i in s.exec(select(PortalItem)).all()
                if i.found_at and _aware(i.found_at) >= start][:5]

        lines = []
        if done:
            lines += ["Done today:", *[f"- {clip(x, 120)}" for x in done[:8]], ""]
        still = open_loops + overdue
        if still:
            lines += ["Still open:", *[f"- {clip(x, 120)}" for x in still], ""]
        if tomorrow:
            lines += ["Tomorrow:", *[f"- {x}" for x in tomorrow], ""]
        if olat:
            lines += ["New on OLAT:", *[f"- {x}" for x in olat], ""]
        if not lines:
            lines = ["A quiet day. Nothing open, nothing new.", ""]
        lines.append("Good night. (Kairo)")
        subject = f"Kairo recap · {local:%A} {local.day} {local:%B}"
        return subject, _plain("\n".join(lines))

    def run(self, now: dt.datetime | None = None) -> dict:
        from . import prefs
        from .gate import TOOL_PREFIX
        from .gate_context import read_context
        now = now or dt.datetime.now(self.tz)
        with Session(self.engine) as s:
            if (prefs.load(s).get("recap") or "off") == "off":     # off until the owner sets a time
                return {"sent": False, "why": "switched off"}
            last = s.get(SettingKV, SENT_KEY)
            today = now.astimezone(self.tz).date().isoformat()
            if last and last.value == today:
                return {"sent": False, "why": "already sent today"}
            subject, body = self.build(s, now)
        d = self.gate.decide(TOOL_PREFIX + "propose_action",
                             {"action": "gmail.send_self", "args": {"subject": subject, "body": body, "label": LABEL},
                              "reason": "evening recap"}, read_context(self.engine, "job"))
        if d.verdict != "execute":
            return {"sent": False, "why": f"gate: {d.reason}"}
        ex = self._executor() if callable(self._executor) else self._executor
        out = ex.run(d.action, d.args)
        with Session(self.engine) as s:
            row = s.get(SettingKV, SENT_KEY) or SettingKV(key=SENT_KEY)
            row.value = today
            s.add(row)
            s.commit()
        return {"sent": True, "sent_id": out.get("sent_id"), "subject": subject}


def as_json(result: dict) -> str:
    return json.dumps(result)
