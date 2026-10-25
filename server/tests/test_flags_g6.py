"""The turn-taking switches (group G6, flags named g6.*): registered with these defaults, both values work through the
owner's API, and the call reads exactly these (voicertc.Turning) with the same defaults. What each one does on a call is
tested in test_voice_rtc.py."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import flags, voicertc
from app.db import make_engine
from app.main import create_app

# name -> default. On: only the plain fixes of measured problems (empty turn, tap). Off: everything that is the owner's
# decision (voice plan section 16 items 1 and 2, gate "owner OK for 6b (both costs); P9, P10") until he switches it on in
# More -> System -> Experiments: the 3 s Smart Turn wait, the ignore windows, the words-only barge-in, and the long-speech
# cut. KICKOFF and ACCEPTANCE row 4 say "talking over Kairo stops it at once"; the words-only barge-in breaks that on
# purpose, so it is not shipped on without his say-so. docs/CHANGELOG.md has their costs in plain words.
G6 = {
    "g6.empty_turn_fix": True,
    "g6.tap_lag_fix": True,
    "g6.smart_turn_3s": True,
    "g6.ignore_windows": True,
    "g6.transcript_barge_in": True,
    "g6.long_speech_cut": False,
}


def test_the_g6_flags_are_exactly_these_with_these_defaults():
    mine = {n: d for n, (d, _w) in flags.FLAGS.items() if n.startswith("g6.")}
    assert mine == G6


@pytest.mark.parametrize("name", sorted(G6))
def test_a_g6_flag_works_on_and_off_and_back_to_its_default(migrated, name):
    with Session(make_engine(migrated.db_url)) as s:
        assert flags.on(s, name) is G6[name]
        for value in (True, False):
            flags.set_flag(s, name, value)
            assert flags.on(s, name) is value
        flags.set_flag(s, name, None)
        assert flags.on(s, name) is G6[name]


@pytest.mark.parametrize("name", sorted(G6))
def test_the_owner_sees_and_flips_a_g6_flag_over_the_api(migrated, name):
    with TestClient(create_app(migrated)) as c:
        row = next(r for r in c.get("/api/flags").json() if r["name"] == name)
        assert row["on"] is G6[name] and row["default"] is G6[name] and row["what"].strip()
        for value in (not G6[name], G6[name]):
            rows = c.put(f"/api/flags/{name}", json={"on": value}).json()
            assert next(r for r in rows if r["name"] == name)["on"] is value
        rows = c.put(f"/api/flags/{name}", json={"on": None}).json()
        assert next(r for r in rows if r["name"] == name)["on"] is G6[name]


def test_the_call_reads_exactly_the_g6_flags_and_its_defaults_are_the_flags_defaults():
    """A call built without the argument (tests, the bench) behaves like a call with every switch at its default."""
    assert set(voicertc.TURN_FLAGS.values()) == set(G6)
    assert voicertc.Turning() == voicertc.Turning.read(lambda name: G6[name])
    asked = []
    voicertc.Turning.read(lambda name: asked.append(name) or True)
    assert sorted(asked) == sorted(G6)


@pytest.mark.parametrize("name", sorted(G6))
def test_turning_follows_each_flag_both_ways(name):
    attr = next(a for a, n in voicertc.TURN_FLAGS.items() if n == name)
    for value in (True, False):
        turning = voicertc.Turning.read(lambda n: value if n == name else G6[n])
        assert getattr(turning, attr) is value
        assert all(getattr(turning, a) is G6[n] for a, n in voicertc.TURN_FLAGS.items() if n != name)
