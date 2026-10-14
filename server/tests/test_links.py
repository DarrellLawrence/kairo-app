"""v8 C7: every note Kairo writes links people, places, subjects, projects and goals ([[wikilinks]])."""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session

from app import links, vault
from app.db import Goal, LocationEvent, Person, Project, SchoolEvent, make_engine
from app.links import Entity
from app.main import create_app

ANNA = Entity("person", "Anna Muster")


def test_first_mention_only_and_the_shown_words_kept():
    text, used = links.link("Call anna muster today. Anna Muster said yes.", [ANNA])
    assert text == "Call [[Anna Muster|anna muster]] today. Anna Muster said yes."
    assert used == [ANNA]


def test_whole_words_only_and_longer_names_first():
    ents = [Entity("subject", "Math"), Entity("project", "Math exam prep")]
    text, _ = links.link("Mathematics is not Math. Start the math exam prep.", ents)
    assert text == "Mathematics is not [[Math]]. Start the [[Math exam prep|math exam prep]]."


def test_code_links_urls_headings_and_markers_are_left_alone():
    src = ("# Anna Muster\n<!-- kairo:anna-muster -->\n`Anna Muster` ```\nAnna Muster\n``` [[Anna Muster]] "
           "[Anna Muster](https://example.com/anna-muster) https://example.com/Anna%20Muster")
    text, used = links.link(src, [ANNA])
    assert text == src and used == []


def test_aliases_from_the_entity_note(tmp_path):
    note = tmp_path / "60 People" / "Anna Muster.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\naliases: [Mama, \"Annie\"]\n---\nmy own text", encoding="utf-8")
    ents = links.entities(_engine(tmp_path, people=["Anna Muster"]), tmp_path)
    text, _ = links.link("Ask Mama about Sunday.", ents)
    assert text == "Ask [[Anna Muster|Mama]] about Sunday."
    assert links.ensure_notes(tmp_path, [e for e in ents if e.kind == "person"]) == []   # hers exists: never touched
    assert note.read_text(encoding="utf-8").endswith("my own text")


def test_missing_notes_are_created_with_frontmatter(tmp_path):
    made = links.ensure_notes(tmp_path, [ANNA, Entity("place", "Gym"), Entity("subject", "Math"),
                                         Entity("goal", "Run 10 km"), Entity("project", "Bike/repair")])
    assert made == ["60 People/Anna Muster.md", "Kairo/Places/Gym.md", "40 School/Math/Math.md", "20 Goals/Run 10 km.md",
                    "30 Projects/Bikerepair.md"]
    text = (tmp_path / "60 People" / "Anna Muster.md").read_text(encoding="utf-8")
    assert text.startswith("---\naliases: []\nkairo: person\n---\n# Anna Muster")


def test_a_note_with_that_name_anywhere_is_used(tmp_path):
    (tmp_path / "00 Inbox").mkdir()
    (tmp_path / "00 Inbox" / "Gym.md").write_text("mine", encoding="utf-8")
    assert links.ensure_notes(tmp_path, [Entity("place", "Gym")]) == []


def test_entities_from_kairos_data(tmp_path):
    (tmp_path / "40 School" / "History").mkdir(parents=True)
    ents = {(e.kind, e.name) for e in links.entities(_engine(tmp_path, people=["Anna Muster", "Al"]), tmp_path)}
    assert ents == {("person", "Anna Muster"), ("place", "Climbing hall"), ("subject", "Math"), ("subject", "History"),
                    ("project", "Bike repair"), ("goal", "Run 10 km")}    # "Al": too short to link safely


def test_no_self_link():
    text, _ = links.link("Anna Muster: birthday in May.", [ANNA], self_rel="60 People/Anna Muster.md")
    assert "[[" not in text


def test_every_note_kairo_writes_is_linked(tmp_path):
    from app.tools import ToolRunner
    root = tmp_path / "vault"
    vault.ensure_structure(root)
    engine = _engine(tmp_path, people=["Anna Muster"])
    vault.LINKERS[root.resolve()] = links.Linker(engine, root)
    try:
        vault.write_section(root, "90 Reviews/2026-W42.md", "review", "Talked to Anna Muster about the trip.")
        assert "[[Anna Muster]]" in (root / "90 Reviews" / "2026-W42.md").read_text(encoding="utf-8")
        assert (root / "60 People" / "Anna Muster.md").is_file()
        tools = ToolRunner(engine=engine, vault_dir=root, google_read=lambda: None)
        tools._notes_write({"path": "trip.md", "content": "Anna Muster books the train to the Climbing hall.", "mode": "overwrite"})
        note = (root / "Kairo" / "trip.md").read_text(encoding="utf-8")
        assert note == "[[Anna Muster]] books the train to the [[Climbing hall]]."   # the model's own notes too
        assert (root / "Kairo" / "Places" / "Climbing hall.md").is_file()
    finally:
        vault.LINKERS.pop(root.resolve(), None)


def test_the_app_links_its_own_vault_only(migrated, tmp_path):
    create_app(migrated)
    assert migrated.vault_dir.resolve() in vault.LINKERS
    other = tmp_path / "elsewhere"
    vault.write_section(other, "00 Inbox/x.md", "a", "Anna Muster")   # no linker for this folder: plain text
    assert "[[" not in (other / "00 Inbox" / "x.md").read_text(encoding="utf-8")


def _engine(tmp_path, *, people):
    from app.db import SQLModel
    engine = make_engine(f"sqlite:///{tmp_path / 'k.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        for n in people:
            s.add(Person(name=n))
        s.add(LocationEvent(place="Climbing hall", event="arrive", ts=dt.datetime(2026, 10, 1, 18, 0, tzinfo=dt.timezone.utc)))
        s.add(SchoolEvent(subject="Math", kind="test", date=dt.datetime(2026, 11, 4, 8, 0, tzinfo=dt.timezone.utc)))
        s.add(Project(title="Bike repair"))
        s.add(Goal(title="Run 10 km", horizon="year"))
        s.add(Goal(title="Old goal", horizon="year", status="done"))
        s.commit()
    return engine


def test_the_cache_is_read_once_and_again_after_invalidate(tmp_path, monkeypatch):
    """time.monotonic() starts near 0 after a boot: an empty cache must never look fresh."""
    engine = _engine(tmp_path, people=["Anna Muster"])
    linker = links.Linker(engine, tmp_path)
    monkeypatch.setattr(links.time, "monotonic", lambda: 5.0)       # 5 s after boot
    assert any(e.name == "Anna Muster" for e in linker.entities())
    from app.db import Person
    with Session(engine) as s:
        s.add(Person(name="Bea Beispiel"))
        s.commit()
    assert not any(e.name == "Bea Beispiel" for e in linker.entities())   # cached for a minute
    linker.invalidate()
    assert any(e.name == "Bea Beispiel" for e in linker.entities())
