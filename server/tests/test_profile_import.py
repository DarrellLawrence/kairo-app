"""v8 G: the owner's profile, only through the app. Stored as-is in 10 Profile/, then the chat model's
reading of it (schema-checked by the gate) creates people, loops, calendar rules, coach facts and exams.
Idempotent, undoable, never touches the owner's own calendar tags. Only made-up data here (the rule:
nothing personal in the repo)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import pytest
from sqlmodel import Session, select

from app import acceptance, capture, prefs, profile_import
from app.acceptance import Turn
from app.db import CalendarTag, Capture, OpenLoop, Person, SchoolEvent, make_engine
from app.google.client import GoogleAPI
from tests.test_acceptance import ctx_for
from tests.test_internal_tools import setup  # noqa: F401  (fixture)
from tests.test_pa import PROFILE, TZ

TODAY = dt.date(2026, 10, 14)
PROFILE_MD = "# Me\n\nApprentice. Sister: Mila (14 March). Climbing club Tuesdays, fixed.\nRenew the passport by 1 December.\n"
APPLY = {"people": [{"name": "Mila", "relation": "sister", "birthday": "--03-14", "notes": "Loves board games"}],
         "loops": [{"what": "Renew the passport", "due": "2026-12-01"}],
         "calendar_rules": [{"match": "Climbing", "type": "FIXED"}, {"match": "Study", "type": "FLEXIBLE"}],
         "coach": {"training_days": ["tue"], "equipment": ["pull-up bar"]},
         "exams": [{"subject": "Chemistry", "kind": "exam", "date": "2026-12-14"}],
         "questions": ["Is Mila your older or younger sister? (guess: younger)"]}


@pytest.fixture
def s(migrated):
    with Session(make_engine(migrated.db_url)) as session:
        yield session


def share(s, migrated, data: bytes = PROFILE_MD.encode(), name: str = "About me.md") -> Capture:
    return capture.store_file(s, migrated.data_dir / "inbox", PROFILE, data=data, content_type="text/markdown", note=None,
                              source="app", file_name=name)


def test_stored_as_is_in_the_profile_folder(s, migrated):
    vault = migrated.data_dir / "vault"
    cap = share(s, migrated)
    out = profile_import.store(s, vault, capture_id=cap.id, today=TODAY)
    path = vault / "10 Profile" / "About me.md"
    assert path.read_bytes() == PROFILE_MD.encode() and out["text"] == PROFILE_MD
    assert out["record"]["path"] == "10 Profile/About me.md" and out["record"]["readable"]
    c = s.get(Capture, cap.id)
    assert c.filed_as == "profile" and json.loads(c.result_json)["line"] == "Saved to your profile: 10 Profile/About me.md."
    again = profile_import.store(s, vault, capture_id=cap.id, today=TODAY)
    assert again["record"]["path"] == "10 Profile/About me.md"                 # the same file: no copy
    other = share(s, migrated, data=b"# Me\n\nSomething else.\n")
    assert profile_import.store(s, vault, capture_id=other.id, today=TODAY)["record"]["path"] == "10 Profile/About me (2).md"
    pasted = profile_import.store(s, vault, text="# Pasted\nHi", today=TODAY)
    assert (vault / pasted["record"]["path"]).read_text(encoding="utf-8") == "# Pasted\nHi"


def test_a_file_already_in_files_moves_to_the_profile(s, migrated):
    vault = migrated.data_dir / "vault"
    filed = vault / "Files" / "Documents" / "About me.md"
    filed.parent.mkdir(parents=True)
    filed.write_text(PROFILE_MD, encoding="utf-8")
    (vault / "Files" / "Documents" / "About me.md.md").write_text("summary", encoding="utf-8")
    cap = Capture(kind="file", raw_path=str(filed), file_name="About me.md", status="filed")
    s.add(cap)
    s.commit()
    profile_import.store(s, vault, capture_id=cap.id, today=TODAY)
    assert not filed.exists() and (vault / "10 Profile" / "About me.md").is_file()


def test_pdf_is_stored_but_not_read(s, migrated):
    cap = capture.store_file(s, migrated.data_dir / "inbox", PROFILE, data=b"%PDF-1.4 fake", content_type="application/pdf",
                             note=None, source="app", file_name="Me.pdf")
    out = profile_import.store(s, migrated.data_dir / "vault", capture_id=cap.id, today=TODAY)
    assert out["text"] is None and not out["record"]["readable"]


def fake_calendar(migrated):
    from tests.fakes.fake_google import FakeGoogle, connect
    fake = FakeGoogle()
    connect(migrated.data_dir / "secrets", fake)
    fake.add_event("e1", summary="Climbing club", start="2026-10-20T18:00:00+02:00")
    fake.add_event("e2", summary="Study block", start="2026-10-21T17:00:00+02:00")
    fake.add_event("e3", summary="Climbing with Sam", start="2026-10-22T18:00:00+02:00")
    return lambda: GoogleAPI(migrated.data_dir / "secrets", "read", transport=fake.transport())


def test_apply_creates_everything_once_and_undo_removes_it(s, migrated):
    vault = migrated.data_dir / "vault"
    s.add(Person(name="Mila", relation="friend", notes="old note"))
    s.add(CalendarTag(event_id="e3", type="SOCIAL"))                          # the owner tagged this one by hand
    s.commit()
    read = fake_calendar(migrated)
    rec = profile_import.store(s, vault, text=PROFILE_MD, today=TODAY)["record"]
    out = profile_import.apply(s, {"import_id": rec["id"], **APPLY}, today=TODAY, tz=TZ, google_read=read,
                               now=dt.datetime(2026, 10, 14, 10, tzinfo=TZ))
    assert out["created"] == {"people": 0, "loops": 1, "exams": 1, "questions": 1}   # Mila existed: updated, not created
    mila = s.exec(select(Person).where(Person.name == "Mila")).one()
    assert mila.relation == "sister" and (mila.birthday.month, mila.birthday.day) == (3, 14) and "Loves board games" in mila.notes
    assert "2 calendar rules (2 events tagged)" in out["added"]
    assert {t.event_id: t.type for t in s.exec(select(CalendarTag)).all()} == {"e1": "FIXED", "e2": "FLEXIBLE", "e3": "SOCIAL"}
    assert prefs.coach(s).training_days == ["tue"] and prefs.coach(s).equipment == ["pull-up bar"]
    loop = s.exec(select(OpenLoop).where(OpenLoop.source == "profile")).one()
    assert loop.what == "Renew the passport" and loop.due is not None
    assert s.exec(select(SchoolEvent)).one().subject == "Chemistry"
    from app import capture as capture_mod
    asked = capture_mod.quick_questions(s)                                    # unclear: asked next time, with a guess
    assert [c.question for c in asked] == ["Is Mila your older or younger sister? (guess: younger)"]
    again = profile_import.apply(s, {"import_id": rec["id"], **APPLY}, today=TODAY, tz=TZ, google_read=read)
    assert again["created"] == {"people": 0, "loops": 0, "exams": 0, "questions": 0}   # the same profile adds nothing twice
    profile_import.undo(s, vault, profile_import._get(s, profile_import.RECORD_KEY.format(rec["id"])))
    s.expire_all()
    mila = s.exec(select(Person).where(Person.name == "Mila")).one()
    assert mila.relation == "friend" and mila.notes == "old note"            # restored as it was
    assert s.exec(select(SchoolEvent)).all() == [] and s.exec(select(OpenLoop).where(OpenLoop.source == "profile")).all() == []
    assert profile_import.rules(s) == [] and prefs.load(s).get("coach") is None and capture_mod.quick_questions(s) == []
    assert not (vault / rec["path"]).exists()


def test_bad_coach_facts_are_reported_not_fatal(s, migrated):
    rec = profile_import.store(s, migrated.data_dir / "vault", text=PROFILE_MD, today=TODAY)["record"]
    with pytest.raises(profile_import.ProfileError):
        profile_import.apply(s, {"import_id": 999}, today=TODAY, tz=TZ)
    out = profile_import.apply(s, {"import_id": rec["id"], "people": [{"name": "Jonas", "relation": "best friend"}]},
                               today=TODAY, tz=TZ)
    assert out["created"]["people"] == 1 and out["added"] == ["1 people"]


def test_the_tools_and_the_chat_prompt(setup, migrated):
    app = setup["app"]
    with Session(setup["engine"]) as s:
        cap = share(s, migrated)
    assert f"#{cap.id} About me.md" in app.state.system_prompt()               # "this" = the file just shared
    r = setup["call"]("profile_import", {"capture_id": cap.id}).json()
    assert not r["is_error"]
    out = json.loads(r["text"])
    assert out["saved"] == "10 Profile/About me.md" and "UNTRUSTED DATA" in out["profile"] and "Mila" in out["profile"]
    r = setup["call"]("profile_apply", {"import_id": out["import_id"], **APPLY}).json()
    assert not r["is_error"] and json.loads(r["text"])["created"]["exams"] == 1
    assert setup["call"]("profile_import", {"capture_id": 99999}).json()["is_error"]
    assert setup["call"]("profile_apply", {"import_id": out["import_id"], "coach": {"quiet_hours": "never"}}).json()["is_error"]


def test_rules_tag_new_events_once_a_day(s, migrated):
    from tests.test_loops import Ctx
    from tests.test_pa import make
    engine, p_, fake, vault_dir = make(migrated)
    fake.add_event("x1", summary="Climbing club", start="2026-10-20T18:00:00+02:00")
    with Session(engine) as s2:
        profile_import._put(s2, profile_import.RULES_KEY, [{"match": "climbing", "type": "FIXED"}])
        assert p_.calendar_rules(Ctx(s2), dt.datetime(2026, 10, 14, 9, tzinfo=TZ)) == {"tagged": 1}
        fake.add_event("x2", summary="Climbing club", start="2026-10-27T18:00:00+02:00")
        assert p_.calendar_rules(Ctx(s2), dt.datetime(2026, 10, 14, 15, tzinfo=TZ)) == {"tagged": 0}   # once a day
        assert p_.calendar_rules(Ctx(s2), dt.datetime(2026, 10, 15, 9, tzinfo=TZ)) == {"tagged": 1}


# ---------------------------------------------------------------- acceptance 20

def test_acceptance_20_passes_and_cleans_up(migrated, s):
    vault = migrated.data_dir / "vault"
    shared = []

    def share_fn(data, name, ctype):
        with Session(make_engine(migrated.db_url)) as s2:
            c = capture.store_file(s2, migrated.data_dir / "inbox", PROFILE, data=data, content_type=ctype, note=None,
                                   source="app", file_name=name)
            shared.append(c.id)
            return c.id

    async def turn(text, session=None):                                       # what the real model does with the tools
        with Session(make_engine(migrated.db_url)) as s2:
            out = profile_import.store(s2, vault, capture_id=shared[-1], today=dt.date.today())
            exam = [ln for ln in out["text"].splitlines() if "Final exam" in ln][0].split(" on ")[-1].rstrip(".")
            profile_import.apply(s2, {"import_id": out["record"]["id"],
                                      "people": [{"name": "KAIRO-TEST Mila", "relation": "sister", "birthday": "--03-14"}],
                                      "loops": [{"what": "Renew my passport", "due": "2026-12-01"}],
                                      "calendar_rules": [{"match": "Climbing club", "type": "FIXED"}],
                                      "coach": {"training_days": ["tue"], "equipment": ["pull-up bar"]},
                                      "exams": [{"subject": "KAIRO-TEST Chemistry", "kind": "exam", "date": exam}]},
                                 today=dt.date.today(), tz=TZ)
        return Turn(reply="Saved to 10 Profile/KAIRO-TEST profile.md.\nAdded: Mila, 1 to-do, 1 calendar rule, training Tuesday, 1 exam.")
    ctx = ctx_for(migrated, turn=turn)
    ctx.share, ctx.vault_dir = share_fn, vault
    prefs.set_key(s, "coach", {"water_l": 2})                                 # the owner's own setting must survive
    r = asyncio.run(acceptance.t_profile_import(ctx))
    assert r["ok"], r
    s.expire_all()
    assert s.exec(select(Person)).all() == [] and s.exec(select(SchoolEvent)).all() == []
    assert s.exec(select(OpenLoop).where(OpenLoop.source == "profile")).all() == [] and profile_import.rules(s) == []
    assert prefs.load(s)["coach"]["water_l"] == 2 and prefs.coach(s).training_days == []
    assert not list((vault / "10 Profile").glob("KAIRO-TEST*")) and s.exec(select(Capture)).all() == []


def test_acceptance_20_fails_when_nothing_is_applied(migrated):
    vault = migrated.data_dir / "vault"

    def share_fn(data, name, ctype):
        with Session(make_engine(migrated.db_url)) as s2:
            return capture.store_file(s2, migrated.data_dir / "inbox", PROFILE, data=data, content_type=ctype, note=None,
                                      source="app", file_name=name).id

    async def turn(text, session=None):
        return Turn(reply="Noted!")
    ctx = ctx_for(migrated, turn=turn)
    ctx.share, ctx.vault_dir = share_fn, vault
    r = asyncio.run(acceptance.t_profile_import(ctx))
    assert not r["ok"] and r["checks"]["stored as-is in 10 Profile/"] is False


def test_no_real_profile_in_the_repo():
    """The owner's profile reaches the PC only through the app: the repo holds only made-up ones."""
    from app.settings import REPO_ROOT
    vault_like = [p for p in REPO_ROOT.rglob("*.md") if "10 Profile" in p.as_posix() and "node_modules" not in p.parts]
    assert vault_like == []
    assert "Nothing here is real" in acceptance.FAKE_PROFILE


def test_places_with_stations_weekdays_and_route_reach_the_today_card(s, migrated):
    from app import pa, prefs
    rec = profile_import.store(s, migrated.data_dir / "vault", text=PROFILE_MD, today=TODAY)["record"]
    places = [{"name": "Home", "kind": "home", "station": "Hometown"},
              {"name": "School", "kind": "school", "station": "Schooltown", "days": ["mon", "tue"]},
              {"name": "Work", "kind": "work", "station": "Worktown", "days": ["wed", "thu", "fri"], "via": "Midtown"}]
    out = profile_import.apply(s, {"import_id": rec["id"], "places": places}, today=TODAY, tz=TZ)
    assert "places: Home, School, Work" in out["added"]
    saved = prefs.places(s)
    monday = dt.datetime(2026, 10, 19, 6, 50, tzinfo=TZ)                      # school days
    wednesday = dt.datetime(2026, 10, 21, 6, 50, tzinfo=TZ)                   # work days, via Midtown
    assert pa.destination(saved, [], monday)[0].name == "School"
    dest = pa.destination(saved, [], wednesday)[0]
    assert dest.name == "Work" and dest.via == "Midtown"
    profile_import.undo(s, None, profile_import.last(s) or rec)
    assert prefs.places(s) == []                                              # undo puts the old places back


def test_the_gate_checks_places():
    from app import actions
    actions.PlaceSet.model_validate({"name": "Work", "kind": "work", "days": ["wed"], "via": "Midtown"})
    for bad in ({"name": "Work", "kind": "work", "days": ["someday"]}, {"name": "W", "kind": "work", "via": "x" * 81}):
        with pytest.raises(Exception):
            actions.PlaceSet.model_validate(bad)
    actions.ProfileApply.model_validate({"import_id": 1, "places": [{"name": "Home", "kind": "home", "station": "A"}]})
