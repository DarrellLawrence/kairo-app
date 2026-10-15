"""Fix plan item 20: one notification boss. Every push goes through Pusher.notify: quiet hours hold a
push until the morning (merged into one), a backup failure goes into the morning brief instead, the
same push twice is dropped, proactive pings share one daily budget, and every decision is logged
(push_log: source, text, sent / held / dropped, why)."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlmodel import Session, select

from app.db import PushLog, PushSubscription, make_engine
from app.push import Pusher
from tests.test_pa import PROFILE, TZ

NIGHT = dt.datetime(2026, 10, 14, 23, 30, tzinfo=TZ)
DAY = dt.datetime(2026, 10, 14, 10, 0, tzinfo=TZ)


class Sent:
    def __init__(self):
        self.payloads = []

    def __call__(self, *, subscription_info, data, **kw):
        self.payloads.append(json.loads(data))


@pytest.fixture
def push(migrated):
    engine = make_engine(migrated.db_url)
    with Session(engine) as s:
        s.add(PushSubscription(endpoint="https://web.push.apple.com/1", keys_json=json.dumps({"p256dh": "k", "auth": "a"})))
        s.commit()
    sent = Sent()
    p = Pusher(engine=engine, secrets_dir=migrated.data_dir / "secrets", profile=PROFILE, sender=sent, max_per_day=lambda: 2)
    p.sent = sent
    return p


def log(p) -> list[tuple]:
    with Session(p.engine) as s:
        return [(r.source, r.status, r.why) for r in s.exec(select(PushLog).order_by(PushLog.id)).all()]


def test_sent_in_the_day_and_logged(push):
    assert push.notify("Kairo", "Approve the reply to Marc?", "/#approvals", source="approval", now=DAY, wait=True) == "sent"
    assert push.sent.payloads == [{"title": "Kairo", "body": "Approve the reply to Marc?", "url": "/#approvals"}]
    assert log(push) == [("approval", "sent", "")]


def test_quiet_hours_hold_until_the_morning_merged(push):
    assert push.notify("Kairo", "Bus 7 is cancelled tomorrow.", source="loops", now=NIGHT) == "held"
    assert push.notify("#4 Approve?", "Reply to Anna", "/#approvals", source="approval", now=NIGHT) == "held"
    assert push.sent.payloads == []
    assert push.flush(now=NIGHT + dt.timedelta(hours=2), wait=True) == 0          # still night
    assert push.flush(now=dt.datetime(2026, 10, 15, 6, 0, tzinfo=TZ), wait=True) == 2
    [one] = push.sent.payloads
    assert one["title"] == "While you slept: 2 things" and "Bus 7 is cancelled" in one["body"] and "Reply to Anna" in one["body"]
    assert [x[1:] for x in log(push)] == [("sent", "after the quiet hours (merged)")] * 2
    assert push.flush(now=dt.datetime(2026, 10, 15, 6, 5, tzinfo=TZ), wait=True) == 0   # once


def test_one_held_push_goes_out_as_it_was(push):
    push.notify("Heads up", "Rain at 7.", "/#today", source="location", now=NIGHT)
    push.flush(now=dt.datetime(2026, 10, 15, 6, 1, tzinfo=TZ), wait=True)
    assert push.sent.payloads == [{"title": "Heads up", "body": "Rain at 7.", "url": "/#today"}]


def test_backup_failure_at_night_goes_into_the_brief(push):
    assert push.notify("Kairo", "Backup failed: disk full", source="backup", now=NIGHT) == "dropped"
    assert log(push) == [("backup", "dropped", "quiet hours: shown at the top of the morning brief")]
    assert push.notify("Kairo", "Backup failed: disk full", source="backup", now=DAY, wait=True) == "sent"


def test_a_call_rings_even_at_night(push):
    assert push.notify("Kairo is calling", "Pick up", source="call", ignore_quiet=True, now=NIGHT, wait=True) == "sent"


def test_the_same_push_twice_is_dropped(push):
    assert push.notify("Kairo", "Water: a glass now.", source="coach", now=DAY, wait=True) == "sent"
    assert push.notify("Kairo", "Water: a glass now.", source="coach", now=DAY + dt.timedelta(hours=1)) == "dropped"
    assert log(push)[-1] == ("coach", "dropped", "the same push was sent less than 6 h ago")
    assert push.notify("Kairo", "Water: a glass now.", source="coach", now=DAY + dt.timedelta(hours=7), wait=True) == "sent"


def test_proactive_pings_share_one_budget_essentials_never_wait(push):
    assert push.notify("Kairo", "a", source="loops", now=DAY, wait=True) == "sent"
    assert push.notify("Kairo", "b", source="coach", now=DAY, wait=True) == "sent"
    assert push.notify("Kairo", "c", source="location", now=DAY) == "dropped"           # 2 a day here
    assert log(push)[-1] == ("location", "dropped", "daily budget of 2 proactive pings used")
    assert push.notify("#9 Approve?", "x", source="approval", now=DAY, wait=True) == "sent"
    assert push.notify("Kairo", "Anna is waiting for your answer", source="reply", now=DAY, wait=True) == "sent"
    assert push.proactive_today(now=DAY) == 2


def test_every_notify_call_names_its_source():
    """No push bypasses the boss: every call in the app says where it comes from."""
    import re
    from app.settings import REPO_ROOT
    bad = []
    for f in (REPO_ROOT / "server" / "app").rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"\.notify\(", text):
            call = text[m.start():text.find("\n\n", m.start())]
            depth, end = 0, None
            for i, ch in enumerate(call):
                depth += ch == "("
                depth -= ch == ")"
                if depth == 0 and ch == ")":
                    end = i
                    break
            if "source=" not in call[:end]:
                bad.append(f"{f.name}:{text[:m.start()].count(chr(10)) + 1}")
    assert bad == []
