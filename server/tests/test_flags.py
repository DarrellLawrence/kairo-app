"""Owner-only switches for changes that wait for the owner's OK or a measurement on the PC (app/flags.py)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import flags
from app.db import make_engine
from app.main import create_app


@pytest.fixture
def two(monkeypatch):
    monkeypatch.setattr(flags, "FLAGS", {"t.fix": (True, "a plain fix"), "t.wait": (False, "waits for the owner")})


def test_a_flag_is_its_default_until_the_owner_sets_it(migrated, two):
    with Session(make_engine(migrated.db_url)) as s:
        assert flags.on(s, "t.fix") is True and flags.on(s, "t.wait") is False
        flags.set_flag(s, "t.wait", True)
        flags.set_flag(s, "t.fix", False)
        assert flags.on(s, "t.fix") is False and flags.on(s, "t.wait") is True
        flags.set_flag(s, "t.fix", None)                        # back to the default
        assert flags.on(s, "t.fix") is True


def test_an_unknown_flag_is_off_and_cant_be_set(migrated, two):
    with Session(make_engine(migrated.db_url)) as s:
        assert flags.on(s, "t.nope") is False
        with pytest.raises(KeyError):
            flags.set_flag(s, "t.nope", True)


def test_the_owner_lists_and_flips_flags(migrated, two):
    with TestClient(create_app(migrated)) as c:
        rows = c.get("/api/flags").json()
        assert rows == [{"name": "t.fix", "on": True, "default": True, "what": "a plain fix"},
                        {"name": "t.wait", "on": False, "default": False, "what": "waits for the owner"}]
        rows = c.put("/api/flags/t.wait", json={"on": True}).json()
        assert next(r for r in rows if r["name"] == "t.wait")["on"] is True
        rows = c.put("/api/flags/t.wait", json={"on": None}).json()
        assert next(r for r in rows if r["name"] == "t.wait")["on"] is False
        assert c.put("/api/flags/t.nope", json={"on": True}).status_code == 404
        assert c.put("/api/flags/t.wait", json={"on": "yes"}).status_code == 422


def test_every_flag_says_what_it_does():
    for name, (default, what) in flags.FLAGS.items():
        assert isinstance(default, bool) and what.strip() and name == name.strip().lower(), name
