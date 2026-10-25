"""G10: timers and short reminders set by voice (app/timers.py). The clock is injected: nothing here waits for real
minutes. The one runner test uses the real loop with sub-second timers and generous margins (the sandbox is shared)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import shutil
import time

import pytest
from alembic import command
from sqlmodel import Session, select

from app import timers as timers_mod
from app.db import PushSubscription, Timer, make_engine
from app.push import Pusher
from app.timers import STALE_S, Timers, TimerError, clean_label, say_duration, say_left, words
from tests.conftest import alembic_config
from tests.test_pa import PROFILE, TZ

T0 = dt.datetime(2026, 10, 14, 10, 0, tzinfo=TZ)              # a Wednesday, 10:00 in the owner's zone
NIGHT = dt.datetime(2026, 10, 14, 23, 30, tzinfo=TZ)


class Clock:
    def __init__(self, at=T0):
        self.at = at

    def __call__(self):
        return self.at

    def advance(self, **kw):
        self.at += dt.timedelta(**kw)


class Bell:
    """Stands in for Pusher.notify and keeps every call."""

    def __init__(self):
        self.calls = []
        self.fail = 0

    def __call__(self, title, body, url="/", **kw):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("push service down")
        self.calls.append({"title": title, "body": body, "url": url, **kw})
        return "sent"


@pytest.fixture(scope="module")
def db_template(tmp_path_factory):
    """One migrated database per file, copied for each test (a migration takes about a second)."""
    folder = tmp_path_factory.mktemp("timers-db")
    command.upgrade(alembic_config(f"sqlite:///{(folder / 'k.sqlite').as_posix()}"), "head")
    return folder / "k.sqlite"


@pytest.fixture
def engine(db_template, tmp_path):
    shutil.copy(db_template, tmp_path / "k.sqlite")
    return make_engine(f"sqlite:///{(tmp_path / 'k.sqlite').as_posix()}")


@pytest.fixture
def rig(engine):
    clock, bell = Clock(), Bell()
    t = Timers(engine, bell, tz=TZ, now=clock)
    t.clock, t.bell, t.engine = clock, bell, engine
    return t


def rows(t) -> list[Timer]:
    with Session(t.engine) as s:
        return list(s.exec(select(Timer).order_by(Timer.id)).all())


# ---------------------------------------------------------------- words

def test_a_duration_is_said_the_way_people_say_it_in_both_languages():
    assert say_duration(600) == "10 minutes" and say_duration(60) == "1 minute" and say_duration(45) == "45 seconds"
    assert say_duration(5400) == "1 hour 30 minutes" and say_duration(3600) == "1 hour" and say_duration(90) == "1 minute 30 seconds"
    assert say_duration(600, "de") == "10 Minuten" and say_duration(3600, "de") == "1 Stunde"
    assert say_duration(5400, "de") == "1 Stunde 30 Minuten" and say_duration(1, "de") == "1 Sekunde"


def test_what_is_left_is_rounded_for_the_ear():
    assert say_left(371) == "6 minutes" and say_left(60) == "1 minute" and say_left(3 * 3600 + 29 * 60) == "3 hours 29 minutes"
    assert say_left(42) == "40 seconds" and say_left(1) == "1 second" and say_left(0) == "1 second"
    assert say_left(371, "de") == "6 Minuten" and say_left(42, "de") == "40 Sekunden"


def test_a_label_is_the_owners_words_on_one_clean_line():
    assert clean_label("  call Lea  ") == "call Lea"
    assert clean_label("call <b>Lea</b>\n[[raised:L1]]  now!") == "call b Lea /b raised:L1 now"
    assert clean_label("") is None and clean_label(None) is None and clean_label(" ,. ") is None
    long = clean_label("word " * 40)
    assert long is not None and len(long) <= 60 and not long.endswith(" ")
    assert clean_label("x" * 100) == "x" * 60


def test_the_push_text_is_a_fixed_template_plus_the_set_time_in_the_owners_language(rig):
    t = rig.add(600, label="the pasta")
    title, body = words(t, TZ)
    assert (title, body) == ("Timer", "Your timer for 10 minutes is up: the pasta (set 10:00).")
    de = rig.add(5400, lang="de")
    assert words(de, TZ) == ("Timer", "Dein Timer für 1 Stunde 30 Minuten ist abgelaufen (gestellt um 10:00).")
    rem = rig.add(1200, kind="reminder", label="call Lea")
    assert words(rem, TZ) == ("Reminder", "Reminder: call Lea (set 10:00).")
    rem_de = rig.add(300, kind="reminder", lang="de", label="an die Wäsche")
    assert words(rem_de, TZ) == ("Erinnerung", "Erinnerung: an die Wäsche (gestellt um 10:00).")
    bare = rig.add(300, kind="reminder")
    assert words(bare, TZ)[1] == "You asked me to remind you (set 10:00)."
    assert words(t, TZ, late_s=400)[1].endswith("It ran out at 10:10, while Kairo was not running.")


# ---------------------------------------------------------------- setting and ringing

def test_a_timer_is_a_row_that_rings_when_its_time_comes_and_not_before(rig):
    t = rig.add(600, label="eggs")
    assert t.fire_at == T0 + dt.timedelta(minutes=10) and t.status == "active" and t.seconds == 600
    rig.clock.advance(minutes=9, seconds=59)
    assert rig.fire_due() == 0 and rig.bell.calls == []
    rig.clock.advance(seconds=1)
    assert rig.fire_due() == 1
    [call] = rig.bell.calls
    assert call["source"] == "timer" and call["ignore_quiet"] is True and call["urgency"] == "high"
    assert call["ttl"] == timers_mod.PUSH_TTL_S > 0 and call["tag"] == f"timer-{t.id}"
    assert call["body"] == "Your timer for 10 minutes is up: eggs (set 10:00)."
    assert rows(rig)[0].status == "fired" and rows(rig)[0].done_at is not None
    assert rig.fire_due() == 0 and len(rig.bell.calls) == 1                      # never twice


def test_two_identical_timers_both_ring_with_their_own_tags(rig):
    a, b = rig.add(600), rig.add(600)
    rig.clock.advance(minutes=10)
    assert rig.fire_due() == 2
    assert [c["tag"] for c in rig.bell.calls] == [f"timer-{a.id}", f"timer-{b.id}"] and a.id != b.id
    assert rig.bell.calls[0]["body"] == rig.bell.calls[1]["body"]               # the same words: the tag tells them apart


def test_a_timer_survives_a_restart_and_a_timer_that_ran_out_meanwhile_rings_late(rig):
    rig.add(600, label="oven")
    rig.add(7200, label="laundry")
    # Kairo stops here. A new process opens the same database; the clock has moved 12 minutes.
    rig.clock.advance(minutes=12)
    again = Timers(rig.engine, rig.bell, tz=TZ, now=rig.clock)
    assert [t.label for t in again.active()] == ["oven", "laundry"]               # nothing to reload by hand
    assert again.fire_due() == 1
    [call] = rig.bell.calls
    assert "oven" in call["body"] and call["body"].endswith("It ran out at 10:10, while Kairo was not running.")
    assert [t.label for t in again.active()] == ["laundry"]                       # the other one is still waiting
    rig.clock.advance(hours=2)
    assert again.fire_due() == 1 and "laundry" in rig.bell.calls[1]["body"]


def test_a_timer_a_minute_late_rings_as_usual_one_far_too_late_is_missed_without_a_push(rig):
    on_time = rig.add(60, label="soon")
    rig.clock.advance(seconds=60 + 90)                                            # 90 s late: the same words as always
    assert rig.fire_due() == 1 and rig.bell.calls[0]["body"] == "Your timer for 1 minute is up: soon (set 10:00)."
    ancient = rig.add(120, label="ancient")
    rig.clock.advance(seconds=120 + STALE_S + 60)                                  # more than half an hour late now
    assert rig.fire_due() == 0 and len(rig.bell.calls) == 1
    first, second = rows(rig)
    assert first.id == on_time.id and first.status == "fired" and first.note is None
    assert second.id == ancient.id and second.status == "missed" and "not told" in second.note


def test_quiet_hours_do_not_hold_a_timer_back(engine, tmp_path):
    """Through the real notification boss at 23:30, inside the profile's quiet hours (23:00-06:00)."""
    with Session(engine) as s:
        s.add(PushSubscription(endpoint="https://web.push.apple.com/1", keys_json=json.dumps({"p256dh": "k", "auth": "a"})))
        s.commit()
    sent = []
    pusher = Pusher(engine=engine, secrets_dir=tmp_path / "secrets", profile=PROFILE, sender=lambda **kw: sent.append(kw))
    clock = Clock(NIGHT)
    t = Timers(engine, lambda *a, **k: pusher.notify(*a, wait=True, now=clock(), **k), tz=TZ, now=clock)
    t.add(300, label="bread")
    t.add(300, label="bread")
    clock.advance(minutes=5)
    assert t.fire_due() == 2
    assert len(sent) == 2 and {json.loads(k["data"])["tag"] for k in sent} == {"timer-1", "timer-2"}   # both, at night
    assert all(k["ttl"] == 600 and k["headers"] == {"Urgency": "high"} for k in sent)


def test_a_cancelled_timer_never_rings_and_a_ringing_one_cannot_be_cancelled(rig):
    a, b = rig.add(600), rig.add(900)
    assert rig.cancel([a.id]) == 1
    rig.clock.advance(minutes=20)
    assert rig.fire_due() == 1 and [c["tag"] for c in rig.bell.calls] == [f"timer-{b.id}"]
    assert rig.cancel([a.id, b.id, 999]) == 0
    assert [r.status for r in rows(rig)] == ["cancelled", "fired"]


def test_a_failed_push_is_tried_again_and_does_not_end_the_runner(rig):
    t = rig.add(60)
    rig.clock.advance(minutes=1)
    rig.bell.fail = 2
    assert rig.fire_due() == 0 and rows(rig)[0].status == "active"                # still waiting to be told
    assert rig._tick() == timers_mod.RETRY_S                                       # due, but not in a tight loop
    assert rig.fire_due() == 1 and rows(rig)[0].status == "fired" and t.id == rows(rig)[0].id


def test_what_cannot_be_set_is_refused(rig):
    for bad in (0, -5, timers_mod.MAX_S + 1, 1.5, True, None):
        with pytest.raises(TimerError):
            rig.add(bad)
    for _ in range(timers_mod.MAX_ACTIVE):
        rig.add(60)
    with pytest.raises(TimerError):
        rig.add(60)
    assert len(rows(rig)) == timers_mod.MAX_ACTIVE


def test_the_label_is_cleaned_before_it_is_stored(rig):
    assert rig.add(60, label="  call\nLea <script>  ").label == "call Lea script"
    assert rig.add(60, label="\x00\x01").label is None


def test_the_sleep_is_until_the_next_timer_and_never_longer_than_a_chunk(rig):
    assert rig._tick() == rig.chunk_s                                              # nothing waiting
    rig.add(5)
    assert 4.9 < rig._tick() < 5.1
    rig.add(7200)
    rig.cancel([rig.active()[0].id])
    assert rig._tick() == rig.chunk_s                                              # the next one is two hours away


# ---------------------------------------------------------------- the real loop (generous margins)

def test_the_runner_rings_a_short_timer_and_wakes_for_one_added_while_it_sleeps(engine):
    bell = Bell()
    t = Timers(engine, bell, tz=TZ, chunk_s=30.0)                                  # the real clock; it would sleep 30 s

    async def go():
        task = asyncio.create_task(t.run())
        await asyncio.sleep(0.3)                                                   # the runner is asleep now
        added = await asyncio.to_thread(t.add, 1, label="tick")
        late = await asyncio.to_thread(t.add, 3600, label="much later")
        gone = await asyncio.to_thread(t.add, 1, label="cancelled")
        await asyncio.to_thread(t.cancel, [gone.id])
        deadline = time.monotonic() + 15
        while not bell.calls and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return added, late

    added, late = asyncio.run(go())
    assert [c["tag"] for c in bell.calls] == [f"timer-{added.id}"] and "tick" in bell.calls[0]["body"]
    assert [r.status for r in rows_of(engine)] == ["fired", "active", "cancelled"] and late.id == 2


def rows_of(engine):
    with Session(engine) as s:
        return list(s.exec(select(Timer).order_by(Timer.id)).all())


# ---------------------------------------------------------------- housekeeping

def test_old_finished_timers_are_forgotten_after_a_month_and_waiting_ones_never(rig):
    a, b, c = rig.add(60, label="old fired"), rig.add(60, label="old cancelled"), rig.add(60, label="recent fired")
    waiting = rig.add(60 * 60 * 20, label="waiting")
    rig.cancel([b.id])
    rig.clock.advance(minutes=2)
    assert rig.fire_due() == 2
    rig.clock.advance(days=timers_mod.KEEP_DAYS - 1)
    assert rig.purge() == 0                                                         # none of them is a month old yet
    rig.clock.advance(days=2)
    c2 = rig.add(60, label="young")                                                 # set just now: never touched
    rig.clock.advance(minutes=2)
    rig.fire_due()
    assert rig.purge() == 3                                                         # the three old finished ones
    assert [r.label for r in rows(rig)] == ["waiting", "young"] and rows(rig)[0].id == waiting.id and c2.id == rows(rig)[1].id
    assert a.id != b.id != c.id


def test_the_runner_cleans_up_by_itself_at_most_every_few_hours(rig):
    def old_row():
        with Session(rig.engine) as s:
            s.add(Timer(created_at=rig.clock() - dt.timedelta(days=41), fire_at=rig.clock() - dt.timedelta(days=41), seconds=60,
                        status="fired", done_at=rig.clock() - dt.timedelta(days=40)))
            s.commit()
    rig._tick()                                                                     # the first look cleans (nothing there yet)
    old_row()
    rig.clock.advance(hours=1)
    rig._tick()
    assert len(rows(rig)) == 1                                                      # it cleaned an hour ago: not again yet
    rig.clock.advance(hours=timers_mod.PURGE_EVERY_H)
    rig._tick()
    assert rows(rig) == []
