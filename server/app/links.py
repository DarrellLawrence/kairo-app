"""v8 C7: the Obsidian graph. Every note Kairo writes links the people, places, school subjects,
projects and goals it mentions with [[wikilinks]]: one note each, created if missing (aliases in
frontmatter), so Obsidian's own graph and backlinks show how things connect. No visual brain in
Kairo itself.

Where: vault.write_section (every backend note) and notes.write (the model's notes) run it.
What counts as an entity comes from Kairo's own data (people, places from location, subjects from
school, active projects and goals) plus the aliases in an entity note's frontmatter, which the owner
can extend. Only the first mention of each is linked; code, existing links, URLs, Markdown links,
headings and Kairo's section markers are left alone. A note with the same name anywhere in the vault
is used as it is (never edited); only a missing one is created.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import Session, select

FOLDERS = {"person": "60 People", "place": "Kairo/Places", "project": "30 Projects", "goal": "20 Goals"}
MIN_NAME = 3
CACHE_S = 60
BAD_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]]')
# Left alone: frontmatter, fenced and inline code, existing [[links]], [text](url) links, bare URLs,
# HTML comments (Kairo's section markers) and headings.
PROTECTED = re.compile(r"\A---\n.*?\n---\n|```.*?```|`[^`\n]*`|\[\[.*?\]\]|\[[^\]\n]*\]\([^)\n]*\)|https?://\S+|<!--.*?-->|^#.*$",
                       re.S | re.M)


@dataclass
class Entity:
    kind: str
    name: str
    aliases: list[str] = field(default_factory=list)

    @property
    def note(self) -> str:
        return BAD_CHARS.sub("", self.name).strip()

    def rel(self) -> str:
        folder = f"40 School/{self.note}" if self.kind == "subject" else FOLDERS[self.kind]
        return f"{folder}/{self.note}.md"


def _frontmatter_aliases(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return []
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    if not m:
        return []
    block = m.group(1)
    inline = re.search(r"^aliases:\s*\[(.*?)\]\s*$", block, re.M)
    if inline:
        return [a.strip().strip("'\"") for a in inline.group(1).split(",") if a.strip()]
    listed = re.search(r"^aliases:\s*\n((?:\s*-\s*.+\n?)+)", block, re.M)
    return [a.strip().lstrip("-").strip().strip("'\"") for a in listed.group(1).splitlines() if a.strip()] if listed else []


def _existing_notes(vault_dir: Path) -> dict[str, Path]:
    """Basename (lower case) -> path, for every note in the vault (hidden folders aside)."""
    out: dict[str, Path] = {}
    for p in vault_dir.rglob("*.md"):
        rel = p.relative_to(vault_dir).parts
        if not any(part.startswith(".") for part in rel):
            out.setdefault(p.stem.lower(), p)
    return out


def entities(engine, vault_dir: Path) -> list[Entity]:
    from . import location
    from .db import Goal, Person, Project, SchoolEvent
    vault_dir = Path(vault_dir)
    found: list[Entity] = []
    with Session(engine) as s:
        found += [Entity("person", n) for n in s.exec(select(Person.name)).all()]
        found += [Entity("place", n) for n in location.places(s)]
        found += [Entity("subject", n) for n in set(s.exec(select(SchoolEvent.subject)).all())]
        found += [Entity("project", t) for t in s.exec(select(Project.title).where(Project.status == "active")).all()]
        found += [Entity("goal", t) for t in s.exec(select(Goal.title).where(Goal.status == "active")).all()]
    school = vault_dir / "40 School"
    if school.is_dir():
        found += [Entity("subject", d.name) for d in school.iterdir() if d.is_dir() and not d.name.startswith(".")]
    notes = _existing_notes(vault_dir) if vault_dir.is_dir() else {}
    out, seen = [], set()
    for e in found:
        key = (e.note or "").lower()
        if len(key) < MIN_NAME or key in seen:
            continue
        seen.add(key)
        existing = notes.get(key)
        e.aliases = [a for a in _frontmatter_aliases(existing) if len(a) >= MIN_NAME] if existing else []
        out.append(e)
    return out


def link(text: str, ents: list[Entity], *, self_rel: str | None = None) -> tuple[str, list[Entity]]:
    """Links the first mention of each entity. Returns (text, the entities it linked)."""
    if not text or not ents:
        return text, []
    spans = [(m.start(), m.end()) for m in PROTECTED.finditer(text)]

    def free(a: int, b: int) -> bool:
        return not any(s < b and a < e for s, e in spans)

    used: list[Entity] = []
    edits: list[tuple[int, int, str]] = []
    # Longer names first, so "Math exam prep" wins over "Math".
    terms = sorted(((t, e) for e in ents for t in [e.name, *e.aliases] if len(t) >= MIN_NAME),
                   key=lambda te: -len(te[0]))
    linked: set[int] = set()
    for term, e in terms:
        if id(e) in linked or (self_rel and e.rel().lower() == self_rel.lower()):
            continue
        for m in re.finditer(r"(?<![\w\[])" + re.escape(term) + r"(?![\w\]])", text, re.I):
            a, b = m.start(), m.end()
            if not free(a, b) or any(s < b and a < x for s, x, _ in edits):
                continue
            shown = m.group(0)
            edits.append((a, b, f"[[{e.note}]]" if shown == e.note else f"[[{e.note}|{shown}]]"))
            linked.add(id(e))
            used.append(e)
            break
    for a, b, new in sorted(edits, reverse=True):
        text = text[:a] + new + text[b:]
    return text, used


def ensure_notes(vault_dir: Path, used: list[Entity]) -> list[str]:
    """Creates the note of each linked entity that has none yet (by name, anywhere in the vault)."""
    vault_dir = Path(vault_dir)
    notes = _existing_notes(vault_dir)
    made = []
    for e in used:
        if e.note.lower() in notes:
            continue
        path = vault_dir / e.rel()
        path.parent.mkdir(parents=True, exist_ok=True)
        aliases = ", ".join(f'"{a}"' for a in e.aliases)
        path.write_text(f"---\naliases: [{aliases}]\nkairo: {e.kind}\n---\n# {e.name}\n\n"
                        "Kairo links here from its notes; Obsidian's backlinks show where.\n", encoding="utf-8")
        notes[e.note.lower()] = path
        made.append(path.relative_to(vault_dir).as_posix())
    return made


class Linker:
    """Registered in vault.LINKERS at start: link(text, rel) for every note Kairo writes."""

    def __init__(self, engine, vault_dir: Path) -> None:
        self.engine, self.vault_dir = engine, Path(vault_dir)
        self._cache: tuple[float, list[Entity]] | None = None   # (monotonic time, entities); None: not read yet
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        """Read the entities again at the next note (a person was just added)."""
        with self._lock:
            self._cache = None

    def entities(self) -> list[Entity]:
        with self._lock:
            if self._cache is None or time.monotonic() - self._cache[0] > CACHE_S:
                self._cache = (time.monotonic(), entities(self.engine, self.vault_dir))
            return self._cache[1]

    def __call__(self, text: str, rel: str | None = None) -> str:
        new, used = link(text, self.entities(), self_rel=rel)
        if used:
            ensure_notes(self.vault_dir, used)
        return new
