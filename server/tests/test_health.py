"""Fix plan 32: Kairo tells the owner what is wrong with itself: once by push, and as Today's chip."""

from __future__ import annotations

import datetime as dt
import json
from collections import namedtuple

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import health, housekeeping
from app.brain import BrainStatus
from app.db import SettingKV, make_engine
from app.main import create_app
from tests.fakes.fake_google import FakeGoogle, connect

Usage = namedtuple("Usage", "total used free")


class Brain:
    def __init__(self, status="ok", tools="connected"):
        self.status, self.tools = status, tools

    def health(self):
        return BrainStatus(self.status)

    def tools_status(self):
        return self.tools


class Runner:
    def __init__(self, brain):
        self.brain = brain


class Pusher:
    def __init__(self):
        self.sent = []

    def notify(self, title, body, url="/", **kw):
        self.sent.append({"title": title, "body": body, "url": url, **kw})
        return "sent"


@pytest.fixture
def watch(migrated, monkeypatch):
    monkeypatch.setattr(health.shutil, "disk_usage", lambda p: Usage(500e9, 100e9, 400e9))
    migrated.vault_dir.mkdir(parents=True, exist_ok=True)
    w = health.HealthWatch(settings=migrated, engine=make_engine(migrated.db_url), runner=Runner(Brain()), pusher=Pusher(),
                           http_get=lambda url: None)
    return w


def test_all_well_is_quiet(watch):
    assert watch.run(full=True) == {}
    assert watch.pusher.sent == []


def test_a_problem_pushes_once_and_shows_until_it_is_gone(watch):
    watch.runner.brain.status = "logged_out"
    problems = watch.run(full=False)
    assert "Kairo Logins" in problems["claude"]
    watch.run(full=False)
    assert len(watch.pusher.sent) == 1
    push = watch.pusher.sent[0]
    assert push["source"] == "health" and push["url"] == "/#more" and "logged out" in push["body"]
    watch.runner.brain.status = "ok"
    assert watch.run(full=False) == {}
    watch.runner.brain.status = "usage_limited"
    watch.run(full=False)
    assert len(watch.pusher.sent) == 2 and "usage limit" in watch.pusher.sent[1]["body"]


def test_tools_and_self_test(watch):
    watch.runner.brain.tools = "failed (kairo: failed)"
    with Session(watch.engine) as s:
        s.add(SettingKV(key="selftest.tools", value=json.dumps({"ok": False, "reason": "voice model missing"})))
        s.commit()
    problems = watch.run(full=False)
    assert "tools are not connected" in problems["tools"] and "voice model missing" in problems["selftest"]


def test_disk_below_10_gb(watch, monkeypatch):
    monkeypatch.setattr(health.shutil, "disk_usage", lambda p: Usage(500e9, 492e9, 8e9))
    assert "8.0 GB free" in watch.run(full=False)["disk"]


def test_syncthing_conflicts(watch):
    note = watch.settings.vault_dir / "10 Profile" / "training.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("Mondays: run", encoding="utf-8")
    (note.parent / "training.sync-conflict-20261014-101530-ABCDEFG.md").write_text("Mondays: swim", encoding="utf-8")
    assert "1 note was changed on two devices" in watch.run(full=False)["conflicts"]


def test_crash_loop(watch):
    now = dt.datetime.now(dt.timezone.utc)
    for i in range(3):
        assert health.record_start(watch.settings.data_dir, now - dt.timedelta(minutes=50 - i)) == i + 1
    assert "crash_loop" not in watch.run(full=False, now=now)
    health.record_start(watch.settings.data_dir, now)
    assert "restarted 4 times" in watch.run(full=False, now=now)["crash_loop"]
    later = now + dt.timedelta(hours=2)
    assert "crash_loop" not in watch.run(full=False, now=later)


def test_google_is_checked_with_a_real_call_every_hour(watch):
    fake = FakeGoogle()
    connect(watch.settings.secrets_dir, fake)
    watch.transport = fake.transport
    assert watch.run(full=True) == {}
    assert any(c["path"] == "/gmail/v1/users/me/profile" for c in fake.calls)    # not just "the file exists"
    fake.revoked = True
    assert watch.run(full=False) == {}                  # the network checks run hourly
    problems = watch.run(full=True)
    assert "signed Kairo out" in problems["google_read"] and "google_write" in problems
    assert len(watch.pusher.sent) == 1                  # one push for both
    assert "google_read" in watch.run(full=False)       # kept between the hourly checks


def test_google_or_network_down_is_not_a_sign_out(watch):
    connect(watch.settings.secrets_dir)
    watch.transport = lambda: httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("offline")))
    assert watch.run(full=True) == {}


def test_tailscale_end_to_end(watch):
    logs = watch.settings.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "boot-check.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"kairo_url": "https://kairo-home.tail1234.ts.net"}).encode())
    asked = []
    watch._http_get = lambda url: asked.append(url)
    assert watch.run(full=True) == {}
    assert asked == ["https://kairo-home.tail1234.ts.net/health"]

    def down(url):
        raise httpx.ConnectError("no route")
    watch._http_get = down
    problems = watch.run(full=True)
    assert "can't reach Kairo" in problems["tailscale"] and "Tailscale" in problems["tailscale"]


def test_no_address_recorded_is_no_alarm(watch):
    watch._http_get = lambda url: (_ for _ in ()).throw(httpx.ConnectError("x"))
    assert "tailscale" not in watch.run(full=True)


def test_todays_chip_and_housekeeping(migrated, monkeypatch):
    monkeypatch.setattr(health.shutil, "disk_usage", lambda p: Usage(500e9, 495e9, 5e9))
    app = create_app(migrated)
    sent = []
    monkeypatch.setattr(app.state.pusher, "notify", lambda title, body, url="/", **k: sent.append((body, k.get("source"))))
    hk = housekeeping.Housekeeping(app)
    hk.health()
    hk.health()                                          # within 5 minutes: not again
    assert len(sent) == 1 and sent[0][1] == "health" and "GB free" in sent[0][0]
    with TestClient(app) as c:
        issues = c.get("/api/today").json()["issues"]
    assert any("GB free" in i for i in issues)
