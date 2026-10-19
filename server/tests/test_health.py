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


def test_the_address_from_the_auto_update_when_the_boot_check_has_none(watch):
    """Acceptance #30: the boot check runs after a boot (days ago); the auto-update writes tailnet.json every
    5 minutes, so its address is there too."""
    data = watch.settings.data_dir
    status = data.parent / "status"
    status.mkdir(parents=True, exist_ok=True)
    tailnet = {"at": 1, "self_relay": "fra", "peers": [], "kairo_url": "https://kairo-home.tail1234.ts.net"}
    (status / "tailnet.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(tailnet).encode())
    (status / "boot-check.json").write_text(json.dumps({"all_ok": True, "checks": []}), encoding="utf-8")   # an old one
    assert health.kairo_url(data) == "https://kairo-home.tail1234.ts.net"
    asked = []
    watch._http_get = lambda url: asked.append(url)
    assert watch.run(full=True) == {} and asked == ["https://kairo-home.tail1234.ts.net/health"]
    (status / "boot-check.json").write_text(json.dumps({"kairo_url": "https://kairo-pc.tail1234.ts.net"}), encoding="utf-8")
    assert health.kairo_url(data) == "https://kairo-pc.tail1234.ts.net"                   # the boot check's comes first
    (status / "boot-check.json").unlink()
    for planted in ("https://evil.example/x", "http://kairo-home.tail1234.ts.net", "https://user@kairo-home.tail1234.ts.net"):
        (status / "tailnet.json").write_text(json.dumps({**tailnet, "kairo_url": planted}), encoding="utf-8")
        assert health.kairo_url(data) is None                                              # only Kairo's own tailnet address


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


def test_starts_the_installer_planned_are_not_a_crash_loop(watch):
    now = dt.datetime.now(dt.timezone.utc)
    note = watch.settings.data_dir.parent / "status" / "planned-restart.json"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_bytes(b"\xef\xbb\xbf" + json.dumps({"at": (now - dt.timedelta(minutes=2)).isoformat()}).encode())
    for _ in range(6):                                   # Kairo Logins stops and starts Kairo a few times
        health.record_start(watch.settings.data_dir, now)
    assert "crash_loop" not in watch.run(full=False, now=now)
    later = now + dt.timedelta(minutes=20)               # the note is old: a start now is a real one
    for _ in range(4):
        health.record_start(watch.settings.data_dir, later)
    assert "restarted 4 times" in watch.run(full=False, now=later)["crash_loop"]


def test_an_auto_update_that_stopped_running_is_a_push(watch):
    """Owner's rule 2026-10-17: an update that fails is never silent. The auto-update notes its own
    failures; a stopped one can't, so Kairo watches its status\\updater.json (written every 5 minutes)."""
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)      # Kairo has been up a while
    note = watch.settings.data_dir.parent / "status" / "updater.json"
    note.parent.mkdir(parents=True, exist_ok=True)
    assert "updates" not in watch.run(full=False, now=now)              # no file yet (older install): no alarm
    note.write_bytes(b"\xef\xbb\xbf" + json.dumps({"at": (now - dt.timedelta(minutes=10)).isoformat()}).encode())
    assert "updates" not in watch.run(full=False, now=now)
    note.write_text(json.dumps({"at": (now - dt.timedelta(hours=3)).astimezone().isoformat()}), encoding="ascii")
    problems = watch.run(full=False, now=now)
    assert "hasn't run for 3 hours" in problems["updates"] and "Update Kairo" in problems["updates"]
    assert len(watch.pusher.sent) == 1 and "auto-update" in watch.pusher.sent[0]["body"]
    woke = now + dt.timedelta(hours=8)                                  # the PC slept: the updater gets its grace
    assert "updates" not in watch.cheap(woke)
    assert "updates" not in watch.cheap(woke + dt.timedelta(minutes=10))     # checked every 5 minutes
    assert "updates" in watch.cheap(woke + dt.timedelta(minutes=21))          # still not run after the grace


def test_an_installed_version_that_isnt_running_is_red_with_a_push(watch, tmp_path):
    """Owner's report 2026-10-19: "Kairo updated to the new version, ok", but the old server kept running."""
    releases = tmp_path / "app" / "releases"
    old = releases / "2026.10.16.1104-aaaaaaa"
    old.mkdir(parents=True)
    (old / "release.json").write_text(json.dumps({"version": "2026.10.16.1104-aaaaaaa"}), encoding="utf-8")
    state = tmp_path / "app" / "state.json"
    state.write_text(json.dumps({"current": "2026.10.16.1104-aaaaaaa"}), encoding="utf-8")
    watch.app_root = old
    now = dt.datetime.now(dt.timezone.utc)
    assert "version" not in watch.run(full=False, now=now)
    state.write_bytes(b"\xef\xbb\xbf" + json.dumps({"current": "2026.10.18.1219-bbbbbbb"}).encode())
    assert "version" not in watch.run(full=False, now=now)                      # an update may be switching
    problems = watch.run(full=False, now=now + dt.timedelta(minutes=11))
    assert "2026.10.18.1219-bbbbbbb is installed" in problems["version"] and "2026.10.16.1104-aaaaaaa" in problems["version"]
    assert len(watch.pusher.sent) == 1 and "didn't take" in watch.pusher.sent[0]["body"]
    state.write_text(json.dumps({"current": "2026.10.16.1104-aaaaaaa"}), encoding="utf-8")
    assert "version" not in watch.run(full=False, now=now + dt.timedelta(minutes=12))
    state.write_text(json.dumps({"current": "2026.10.15.0900-ccccccc"}), encoding="utf-8")   # newer one under test
    assert "version" not in watch.run(full=False, now=now + dt.timedelta(minutes=40))


def test_health_says_which_version_answers(migrated):
    """The installer counts an update only once the new version itself answers on the port."""
    from fastapi.testclient import TestClient
    with TestClient(create_app(migrated)) as c:
        body = c.get("/health").json()
    assert body["version"] and isinstance(body["pid"], int)

