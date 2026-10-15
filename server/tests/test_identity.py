"""Who the owner is (loops block L, owner's request 2026-10-15). Written before the code.

Kairo must know every name and address that means the owner, and never make a loop, a draft or a
card addressed to them. The values come from the PC (profile.yaml there, Google), never the repo:
these tests use a made-up owner.
"""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session

from app import identity
from app.google.client import GoogleAPI
from app.profile import Profile
from tests.fakes.fake_google import FakeGoogle, connect

OWNER = Profile(name="Sam", default_language="en", languages=["en", "de"], timezone="Europe/Zurich", context="",
                gmail="sam.rivera@example.com", quiet_hours={"start": "22:00", "end": "07:00"},
                emails=["sam@work.example"], aliases=["Sammy"])


def read_for(env_secrets, fake):
    connect(env_secrets, fake)
    return lambda: GoogleAPI(env_secrets, "read", transport=fake.transport())


def test_profile_names_and_addresses_are_the_owner(migrated):
    with Session(identity_engine(migrated)) as s:
        me = identity.load(s, OWNER)
    assert me.is_me(email="Sam.Rivera@Example.com") and me.is_me(email="sam@work.example")
    assert me.is_me(name="Sammy") and me.is_me(name="sam")
    assert not me.is_me(email="lea@example.com") and not me.is_me(name="Lea Muster")


def test_google_adds_every_send_as_address_and_the_full_name(migrated):
    fake = FakeGoogle(owner="sam.rivera@example.com")
    fake.send_as = [{"sendAsEmail": "sam.rivera@example.com", "displayName": "Sam Rivera", "isPrimary": True},
                    {"sendAsEmail": "s.rivera@school.example", "displayName": "Sam Rivera"}]
    read = read_for(migrated.secrets_dir, fake)
    with Session(identity_engine(migrated)) as s:
        me = identity.load(s, OWNER, google_read=read)
        assert me.is_me(email="s.rivera@school.example") and me.is_me(name="Sam Rivera")
        assert me.is_me(name="Rivera, Sam") and me.is_me(name="SAM RIVERA")
        fake.revoked = True                                            # Google down later: what it knew stays
        again = identity.load(s, OWNER, google_read=read, refresh=True)
    assert again.is_me(email="s.rivera@school.example")


def test_a_first_name_alone_is_the_owner_only_without_an_address(migrated):
    with Session(identity_engine(migrated)) as s:
        me = identity.load(s, OWNER)
    assert me.is_me(name="Sam")
    assert not me.is_me(name="Sam", email="sam.other@example.com")    # another Sam, with their own address


def test_others_keeps_the_people_and_drops_the_owner(migrated):
    with Session(identity_engine(migrated)) as s:
        me = identity.load(s, OWNER)
    people = identity.people_in("Sam Rivera <sam.rivera@example.com>, Lea Muster <lea@example.com>",
                                "Tom <tom@example.com>, sam@work.example")
    assert me.others(people) == [{"name": "Lea Muster", "email": "lea@example.com"}, {"name": "Tom", "email": "tom@example.com"}]


def test_the_identity_is_refreshed_once_a_day(migrated):
    fake = FakeGoogle(owner="sam.rivera@example.com")
    read = read_for(migrated.secrets_dir, fake)
    t0 = dt.datetime(2026, 10, 15, 9, tzinfo=dt.timezone.utc)
    with Session(identity_engine(migrated)) as s:
        identity.load(s, OWNER, google_read=read, now=t0)
        n = len(fake.calls)
        identity.load(s, OWNER, google_read=read, now=t0 + dt.timedelta(hours=3))
        assert len(fake.calls) == n                                    # cached
        identity.load(s, OWNER, google_read=read, now=t0 + dt.timedelta(hours=25))
        assert len(fake.calls) > n


def identity_engine(settings):
    from app.db import make_engine
    return make_engine(settings.db_url)
