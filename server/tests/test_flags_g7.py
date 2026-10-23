"""The phone-side switches (group G7, flags named g7.*): registered with the defaults the phone also has, and
both values work through the owner's API. What each one does on the phone is tested in web/tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import flags
from app.db import make_engine
from app.main import create_app

# name -> default. On by default: plain fixes of problems measured in the research (docs/CHANGELOG.md says which).
G7 = {
    "g7.mic_constraints": True,
    "g7.call_audio_mode": True,
    "g7.call_logs": True,
    "g7.no_aec_warning": True,
    "g7.listen_on_barge_in": True,
}


def test_the_g7_flags_are_exactly_these_with_these_defaults():
    mine = {n: d for n, (d, _w) in flags.FLAGS.items() if n.startswith("g7.")}
    assert mine == G7


@pytest.mark.parametrize("name", sorted(G7))
def test_a_g7_flag_works_on_and_off_and_back_to_its_default(migrated, name):
    with Session(make_engine(migrated.db_url)) as s:
        assert flags.on(s, name) is G7[name]
        for value in (True, False):
            flags.set_flag(s, name, value)
            assert flags.on(s, name) is value
        flags.set_flag(s, name, None)
        assert flags.on(s, name) is G7[name]


@pytest.mark.parametrize("name", sorted(G7))
def test_the_owner_sees_and_flips_a_g7_flag_over_the_api(migrated, name):
    with TestClient(create_app(migrated)) as c:
        row = next(r for r in c.get("/api/flags").json() if r["name"] == name)
        assert row["on"] is G7[name] and row["default"] is G7[name] and row["what"].strip()
        for value in (not G7[name], G7[name]):
            rows = c.put(f"/api/flags/{name}", json={"on": value}).json()
            assert next(r for r in rows if r["name"] == name)["on"] is value
        rows = c.put(f"/api/flags/{name}", json={"on": None}).json()
        assert next(r for r in rows if r["name"] == name)["on"] is G7[name]
