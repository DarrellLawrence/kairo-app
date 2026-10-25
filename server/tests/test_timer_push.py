"""G10: a timer's push must still arrive when the phone is asleep or out of reach for a while, and two timers must
never swallow each other. RFC 8030: a push with a time-to-live of 0 is dropped by the push service when the phone is
unreachable at that moment, and pywebpush defaults to 0; "Urgency: high" asks it to wake the phone. Every other push
(approvals, replies, system) is sent exactly as before."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlmodel import Session, select

from app import outbound
from app.db import PushLog, PushSubscription, make_engine
from app.push import Pusher
from tests.test_pa import PROFILE, TZ

NIGHT = dt.datetime(2026, 10, 14, 23, 30, tzinfo=TZ)
DAY = dt.datetime(2026, 10, 14, 10, 0, tzinfo=TZ)


class Sent:
    def __init__(self):
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)

    @property
    def payloads(self):
        return [json.loads(c["data"]) for c in self.calls]


@pytest.fixture
def push(migrated):
    engine = make_engine(migrated.db_url)
    with Session(engine) as s:
        s.add(PushSubscription(endpoint="https://web.push.apple.com/1", keys_json=json.dumps({"p256dh": "k", "auth": "a"})))
        s.commit()
    sent = Sent()
    p = Pusher(engine=engine, secrets_dir=migrated.data_dir / "secrets", profile=PROFILE, sender=sent)
    p.sent = sent
    return p


def test_a_timer_push_has_a_time_to_live_urgency_high_and_its_own_tag(push):
    assert push.notify("Timer", "Your timer for 10 minutes is up (set 09:50).", "/", source="timer", now=DAY, wait=True,
                       ttl=600, urgency="high", tag="timer-7") == "sent"
    [call] = push.sent.calls
    assert call["ttl"] == 600 and call["headers"] == {"Urgency": "high"}
    assert push.sent.payloads == [{"title": "Timer", "body": "Your timer for 10 minutes is up (set 09:50).", "url": "/",
                                   "tag": "timer-7"}]


def test_every_other_push_goes_out_exactly_as_before(push):
    push.notify("Kairo", "Approve the reply to Marc?", "/#approvals", source="approval", now=DAY, wait=True)
    [call] = push.sent.calls
    assert "ttl" not in call and "headers" not in call                      # pywebpush's own defaults, as before
    assert push.sent.payloads == [{"title": "Kairo", "body": "Approve the reply to Marc?", "url": "/#approvals"}]


def test_two_identical_timers_are_both_pushed_but_the_same_one_twice_is_not(push):
    body = "Your timer for 10 minutes is up (set 09:50)."
    assert push.notify("Timer", body, source="timer", now=DAY, wait=True, tag="timer-1") == "sent"
    assert push.notify("Timer", body, source="timer", now=DAY, wait=True, tag="timer-2") == "sent"   # same words, other timer
    assert push.notify("Timer", body, source="timer", now=DAY, wait=True, tag="timer-2") == "dropped"  # a retry of the same one
    assert [p["tag"] for p in push.sent.payloads] == ["timer-1", "timer-2"]


def test_a_timer_rings_in_the_night_when_it_says_so(push):
    assert push.notify("Timer", "up", source="timer", now=NIGHT, wait=True, ignore_quiet=True, ttl=600) == "sent"
    assert push.notify("Timer", "up too", source="timer", now=NIGHT) == "held"          # the default is unchanged


def test_the_tag_is_short_and_urgency_is_one_of_the_four_the_rfc_names(push):
    push.notify("Timer", "a", source="timer", now=DAY, wait=True, tag="t" * 200, urgency="high")
    push.notify("Timer", "b", source="timer", now=DAY, wait=True, tag="x", urgency="whenever")
    assert len(push.sent.payloads[0]["tag"]) <= 64
    assert push.sent.calls[0]["headers"] == {"Urgency": "high"} and "headers" not in push.sent.calls[1]


def test_a_non_positive_or_silly_ttl_is_not_sent(push):
    for ttl in (0, -5, None):
        push.sent.calls.clear()
        push.notify("Timer", f"t{ttl}", source="timer", now=DAY, wait=True, ttl=ttl)
        assert "ttl" not in push.sent.calls[0]
    push.sent.calls.clear()
    push.notify("Timer", "huge", source="timer", now=DAY, wait=True, ttl=10 ** 9)
    assert push.sent.calls[0]["ttl"] <= 7 * 24 * 3600                       # capped (the services cap it anyway)


def test_the_push_destinations_are_still_the_browsers_push_services_only(push):
    """The timer work adds a header and a number to the same request: the host check is untouched."""
    with Session(push.engine) as s:
        s.add(PushSubscription(endpoint="https://evil.example/x", keys_json=json.dumps({"p256dh": "k", "auth": "a"})))
        s.commit()
    push.notify("Timer", "x", source="timer", now=DAY, wait=True, ttl=600, urgency="high", tag="timer-1")
    assert [c["subscription_info"]["endpoint"] for c in push.sent.calls] == ["https://web.push.apple.com/1"]
    assert "push.py" in outbound.MODULES and not any("timers" in m for m in outbound.MODULES)


def test_the_real_library_puts_the_ttl_and_the_urgency_into_the_request(push, tmp_path, monkeypatch):
    """pywebpush 2.5.0 itself (curl=True builds the request and sends nothing): the headers a push service reads."""
    monkeypatch.chdir(tmp_path)                                            # curl=True writes the body to ./encrypted.data
    import base64
    import os

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from pywebpush import webpush

    def b64(b):
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    pub = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    sub = {"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh": b64(pub), "auth": b64(os.urandom(16))}}
    info = {"subscription_info": sub, "data": "{}", "vapid_private_key": str(push.pem), "vapid_claims": {"sub": "mailto:a@b.c"}}
    timer = webpush(curl=True, ttl=600, headers={"Urgency": "high"}, **info).lower()
    plain = webpush(curl=True, **info).lower()
    assert "ttl: 600" in timer
    assert "urgency: high" in timer
    assert "ttl: 0" in plain and "urgency" not in plain                    # today's pushes: ttl 0, no urgency


# ---------------------------------------------------------------- did it go out? (found by the review of G10)

class Down:
    """A sender that fails the way pywebpush does: with an exception (a timeout, or a push service that says 404 / 410 / 5xx)."""

    def __init__(self, error=None, status=None):
        self.error, self.status, self.calls = error or TimeoutError("push service timed out"), status, 0

    def __call__(self, **kw):
        self.calls += 1
        if self.status is not None:
            err = RuntimeError(f"Push failed: {self.status}")
            err.response = type("R", (), {"status_code": self.status})()
            raise err
        raise self.error


def pushes(push):
    with Session(push.engine) as s:
        return [(r.status, r.why) for r in s.exec(select(PushLog).order_by(PushLog.id)).all()]


def test_a_push_can_say_how_many_devices_took_it(push):
    report = {}
    assert push.notify("Timer", "up", source="timer", now=DAY, ttl=600, tag="timer-1", report=report) == "sent"
    assert report == {"tried": 1, "delivered": 1, "error": None} and len(push.sent.calls) == 1        # report waits for the answer
    assert pushes(push) == [("sent", "")]


def test_a_push_nobody_took_is_marked_failed_with_the_reason_and_may_be_tried_again(push):
    push._send = Down()
    report = {}
    assert push.notify("Timer", "up", source="timer", now=DAY, tag="timer-1", report=report) == "sent"
    assert (report["tried"], report["delivered"]) == (1, 0) and "timed out" in report["error"]
    [(status, why)] = pushes(push)
    assert status == "failed" and "timed out" in why                                                  # push_log tells the truth
    push._send = Down()
    again = {}
    assert push.notify("Timer", "up", source="timer", now=DAY, tag="timer-1", report=again) == "sent"  # not "the same push, 6 h"
    assert again["tried"] == 1 and push._send.calls == 1
    push._send = push.sent                                                                            # and then it goes out
    done = {}
    assert push.notify("Timer", "up", source="timer", now=DAY, tag="timer-1", report=done) == "sent"
    assert done["delivered"] == 1 and [s for s, _ in pushes(push)] == ["failed", "failed", "sent"]
    assert push.notify("Timer", "up", source="timer", now=DAY, tag="timer-1", report={}) == "dropped"   # delivered once: not twice


def test_a_phone_the_push_service_has_forgotten_counts_as_not_delivered(push):
    push._send = Down(status=410)
    report = {}
    push.notify("Timer", "up", source="timer", now=DAY, report=report)
    assert (report["tried"], report["delivered"]) == (1, 0) and "410" in report["error"]
    with Session(push.engine) as s:
        assert s.exec(select(PushSubscription)).all() == []                                           # as before: it unsubscribed


def test_with_no_device_at_all_nothing_is_tried_and_it_is_said(push):
    with Session(push.engine) as s:
        for sub in s.exec(select(PushSubscription)).all():
            s.delete(sub)
        s.commit()
    report = {}
    push.notify("Timer", "up", source="timer", now=DAY, report=report)
    assert (report["tried"], report["delivered"]) == (0, 0) and "no device" in report["error"]
    assert pushes(push)[0][0] == "failed" and push.sent.calls == []


def test_one_device_is_enough_and_the_log_stays_sent(push):
    with Session(push.engine) as s:
        s.add(PushSubscription(endpoint="https://fcm.googleapis.com/fcm/send/2", keys_json=json.dumps({"p256dh": "k", "auth": "a"})))
        s.commit()
    calls = []

    def one_of_two(**kw):
        calls.append(kw["subscription_info"]["endpoint"])
        if "fcm" in calls[-1]:
            raise TimeoutError("push service timed out")

    push._send = one_of_two
    report = {}
    push.notify("Timer", "up", source="timer", now=DAY, report=report)
    assert (report["tried"], report["delivered"]) == (2, 1) and len(calls) == 2
    assert pushes(push) == [("sent", "")]


def test_a_push_nobody_asked_a_report_for_is_logged_and_sent_exactly_as_before(push):
    push._send = Down()
    assert push.notify("Kairo", "Approve?", "/#approvals", source="approval", now=DAY, wait=True) == "sent"
    assert pushes(push) == [("sent", "")]                                                             # as it always was
