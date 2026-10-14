"""Config in layers (fix P0.4). The release brings the defaults (config/), data/config holds only
what the owner changed, and Kairo builds the config it runs with at every start. Real case
(diagnosis 2026-10-14): config was copied once at the first install and never updated, so
kairo-home ran without 23 of 38 actions. Written before config_layers.py."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from app import config_layers as cl
from app.gate import load_gate
from app.profile import load_profile
from app.settings import REPO_ROOT

DEFAULTS = REPO_ROOT / "config"


def defaults_copy(tmp: Path) -> Path:
    d = tmp / "release-config"
    shutil.copytree(DEFAULTS, d)
    return d


def read_yaml(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def write_yaml(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def fresh_overrides(tmp: Path) -> Path:
    o = tmp / "data" / "config"
    o.mkdir(parents=True)
    (o / cl.MARKER).write_text("1")
    return o


def build(tmp: Path, overrides: Path, defaults: Path = DEFAULTS) -> tuple[Path, dict]:
    out = tmp / "data" / "config-effective"
    report = cl.build(defaults, overrides, out, backup_root=tmp / "data" / "backups")
    return out, report


# ---------------------------------------------------------------- permissions: stricter only

def test_no_overrides_means_the_release_defaults(tmp_path):
    out, _ = build(tmp_path, fresh_overrides(tmp_path))
    assert read_yaml(out / "permissions.yaml")["actions"] == read_yaml(DEFAULTS / "permissions.yaml")["actions"]
    assert load_profile(out) == load_profile(DEFAULTS)
    assert (out / "personality.md").read_text(encoding="utf-8") == (DEFAULTS / "personality.md").read_text(encoding="utf-8")
    load_gate(out)


def test_owner_can_make_actions_stricter_but_never_looser(tmp_path):
    o = fresh_overrides(tmp_path)
    write_yaml(o / "permissions.yaml", {
        "default": "allow",                              # only the release decides
        "tiers": {"3_outbound": {"mode": "auto"}},       # only the release decides
        "actions": {
            "gmail.send": "0_read",                      # looser: ignored
            "gmail.label": "3_outbound",                 # stricter: kept
            "gmail.trash": "deny",                       # off: kept
            "calendar.move": "5_money",                  # "never": the action is gone
            "gmail.forward_all": "3_outbound",           # not an action of this release: ignored
            "tasks.create": "banana",                    # not a tier: ignored
        },
        "limits": {"approvals_open_max": 3, "outbound_per_day_max": 500},
    })
    out, report = build(tmp_path, o)
    perms = read_yaml(out / "permissions.yaml")
    assert perms["default"] == "deny"
    assert perms["tiers"] == read_yaml(DEFAULTS / "permissions.yaml")["tiers"]
    assert perms["actions"]["gmail.send"] == "3_outbound"
    assert perms["actions"]["gmail.label"] == "3_outbound"
    assert "gmail.trash" not in perms["actions"] and "calendar.move" not in perms["actions"]
    assert "gmail.forward_all" not in perms["actions"]
    assert perms["actions"]["tasks.create"] == "1_internal"
    assert perms["limits"] == {"approvals_open_max": 3, "outbound_per_day_max": 20}
    gate = load_gate(out)
    assert gate.actions["gmail.label"] == 3 and "gmail.trash" not in gate.actions
    joined = " | ".join(report["ignored"])
    for word in ("gmail.send", "gmail.forward_all", "banana", "outbound_per_day_max", "default", "tiers"):
        assert word in joined, joined


def test_new_actions_come_with_the_release(tmp_path):
    o = fresh_overrides(tmp_path)
    write_yaml(o / "permissions.yaml", {"actions": {"gmail.label": "3_outbound"}})
    release = defaults_copy(tmp_path)
    out, _ = build(tmp_path, o, release)
    before = set(read_yaml(out / "permissions.yaml")["actions"])
    perms = read_yaml(release / "permissions.yaml")
    perms["actions"].pop("calendar.tag")                 # the "older release" lacked it
    write_yaml(release / "permissions.yaml", perms)
    out, report = build(tmp_path, o, release)
    assert "calendar.tag" not in read_yaml(out / "permissions.yaml")["actions"] and report["new_actions"] == []
    out, report = build(tmp_path, o, DEFAULTS)           # the update brings it back
    assert set(read_yaml(out / "permissions.yaml")["actions"]) == before
    assert report["new_actions"] == ["calendar.tag"]


# ---------------------------------------------------------------- yaml and text files

def test_profile_override_keeps_new_keys_from_the_release(tmp_path):
    o = fresh_overrides(tmp_path)
    write_yaml(o / "profile.yaml", {"context": "owner's own words", "quiet_hours": {"start": "22:30"}})
    out, _ = build(tmp_path, o)
    p = read_yaml(out / "profile.yaml")
    d = read_yaml(DEFAULTS / "profile.yaml")
    assert p["context"] == "owner's own words"
    assert p["quiet_hours"] == {"start": "22:30", "end": d["quiet_hours"]["end"]}
    assert p["school_portal"] == d["school_portal"] and p["places"] == d["places"]
    load_profile(out)


def test_text_files_override_whole(tmp_path):
    o = fresh_overrides(tmp_path)
    (o / "voice.md").write_text("my voice", encoding="utf-8")
    out, _ = build(tmp_path, o)
    assert (out / "voice.md").read_text(encoding="utf-8") == "my voice"
    assert (out / "personality.md").read_bytes() == (DEFAULTS / "personality.md").read_bytes()
    assert not (out / cl.HISTORY).exists() and not (out / cl.MARKER).exists()


def test_effective_folder_is_rebuilt_not_patched(tmp_path):
    o = fresh_overrides(tmp_path)
    (o / "voice.md").write_text("mine", encoding="utf-8")
    out, _ = build(tmp_path, o)
    (o / "voice.md").unlink()                            # the owner went back to the default
    out, _ = build(tmp_path, o)
    assert (out / "voice.md").read_bytes() == (DEFAULTS / "voice.md").read_bytes()


# ---------------------------------------------------------------- migration of the old full copies

def history() -> dict:
    return json.loads((DEFAULTS / cl.HISTORY).read_text(encoding="utf-8"))


OLD_END = "05:30"


def old_install(tmp: Path) -> tuple[Path, Path]:
    """data/config as kairo-home has it: full copies from the first install, a few owner edits.
    Returns (data/config, the release's config folder)."""
    release = defaults_copy(tmp)
    h = history()
    older = read_yaml(DEFAULTS / "profile.yaml")
    older["quiet_hours"]["end"] = OLD_END               # a default that has changed since
    cl.add_to_history(h, "profile.yaml", yaml.safe_dump(older).encode())
    (release / cl.HISTORY).write_text(json.dumps(h), encoding="utf-8")

    o = tmp / "data" / "config"
    o.mkdir(parents=True)
    for f in DEFAULTS.iterdir():
        if f.name != cl.HISTORY:
            shutil.copy(f, o / f.name)
    perms = read_yaml(o / "permissions.yaml")
    for a in list(perms["actions"])[-23:]:              # the first release had far fewer actions
        perms["actions"].pop(a)
    perms["actions"]["gmail.search"] = "1_internal"     # owner made one stricter
    write_yaml(o / "permissions.yaml", perms)
    prof = read_yaml(o / "profile.yaml")
    prof.pop("school_portal")                            # did not exist yet
    prof["context"] = "owner edited this"
    prof["quiet_hours"]["end"] = OLD_END                 # untouched by the owner, old default
    write_yaml(o / "profile.yaml", prof)
    sched = read_yaml(o / "schedule.yaml")
    sched["reviews"]["daily"] = "20:45"                  # owner edit
    write_yaml(o / "schedule.yaml", sched)
    (o / "personality.md").write_text((DEFAULTS / "personality.md").read_text(encoding="utf-8").replace("\n", "\r\n"),
                                      encoding="utf-8", newline="")   # opened and saved in Notepad
    (o / "voice.md").write_text("owner's voice notes\n", encoding="utf-8")
    (o / "extra-notes.txt").write_text("keep me", encoding="utf-8")
    return o, release


def test_migration_keeps_only_what_the_owner_changed(tmp_path):
    o, release = old_install(tmp_path)
    original = {f.name: f.read_bytes() for f in o.iterdir()}
    out, report = build(tmp_path, o, release)
    assert report["migrated"] is True
    # a full backup of what was there
    backups = list((tmp_path / "data" / "backups").glob("config-before-overrides-*"))
    assert len(backups) == 1 and {f.name: f.read_bytes() for f in backups[0].iterdir()} == original
    # deltas only
    assert read_yaml(o / "permissions.yaml") == {"actions": {"gmail.search": "1_internal"}}
    assert read_yaml(o / "profile.yaml") == {"context": "owner edited this"}
    assert read_yaml(o / "schedule.yaml") == {"reviews": {"daily": "20:45"}}
    assert not (o / "personality.md").exists()          # only line endings differed: the release's applies
    assert (o / "voice.md").read_text(encoding="utf-8") == "owner's voice notes\n"
    assert (o / "extra-notes.txt").read_text(encoding="utf-8") == "keep me"
    assert (o / cl.MARKER).is_file() and (o / "README.txt").is_file()
    # nothing the owner set is lost, everything else is the release's
    eff_perms = read_yaml(out / "permissions.yaml")["actions"]
    assert eff_perms["gmail.search"] == "1_internal"
    assert set(eff_perms) == set(read_yaml(DEFAULTS / "permissions.yaml")["actions"])
    assert len(report["new_actions"]) == 23
    prof = read_yaml(out / "profile.yaml")
    assert prof["context"] == "owner edited this" and prof["school_portal"] == read_yaml(DEFAULTS / "profile.yaml")["school_portal"]
    assert prof["quiet_hours"]["end"] == read_yaml(DEFAULTS / "profile.yaml")["quiet_hours"]["end"] != OLD_END   # new default arrived
    assert read_yaml(out / "schedule.yaml")["reviews"]["daily"] == "20:45"
    assert (out / "voice.md").read_text(encoding="utf-8") == "owner's voice notes\n"
    load_gate(out)
    load_profile(out)


def test_migration_runs_once(tmp_path):
    o, release = old_install(tmp_path)
    build(tmp_path, o, release)
    write_yaml(o / "profile.yaml", {"context": "owner edited this", "name": read_yaml(DEFAULTS / "profile.yaml")["name"]})
    _, report = build(tmp_path, o, release)
    assert report["migrated"] is False
    assert "name" in read_yaml(o / "profile.yaml")       # an explicit override equal to the default stays


def test_fresh_install_gets_marker_and_readme(tmp_path):
    o = tmp_path / "data" / "config"
    o.mkdir(parents=True)
    out, report = build(tmp_path, o)
    assert report["migrated"] is False and (o / cl.MARKER).is_file() and (o / "README.txt").is_file()
    assert report["new_actions"] == []                   # nothing "new" on a first install


# ---------------------------------------------------------------- the defaults history (migration needs it)

@pytest.mark.parametrize("f", sorted(p.name for p in DEFAULTS.iterdir() if p.name != cl.HISTORY))
def test_every_current_default_is_in_the_history(f):
    """Change a default -> run scripts/config_history.py (else an old install would pin the old value)."""
    assert "surname" not in (DEFAULTS / cl.HISTORY).read_text(encoding="utf-8").lower()   # fingerprints only
    h = history()
    if f.endswith(".yaml"):
        for path, value in cl.flatten(read_yaml(DEFAULTS / f)).items():
            assert cl.value_key(value) in h["yaml"][f].get(cl.path_key(path), []), \
                f"{f} {path}: run scripts/config_history.py"
    else:
        assert cl.text_digest((DEFAULTS / f).read_bytes()) in h["text"][f], f"{f}: run scripts/config_history.py"


# ---------------------------------------------------------------- wiring

def test_settings_with_overrides_run_on_the_effective_folder(tmp_path, monkeypatch):
    from app.settings import load_settings
    monkeypatch.setenv("KAIRO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KAIRO_CONFIG_DIR", str(tmp_path / "legacy"))
    monkeypatch.setenv("KAIRO_CONFIG_OVERRIDES", str(tmp_path / "data" / "config"))
    s = load_settings()
    assert s.config_dir == tmp_path / "data" / "config-effective"
    assert s.config_overrides == tmp_path / "data" / "config"
    monkeypatch.delenv("KAIRO_CONFIG_OVERRIDES")
    assert load_settings().config_dir == tmp_path / "legacy" and load_settings().config_overrides is None


def test_app_start_builds_config_and_notes_new_actions_once(settings, tmp_path):
    from fastapi.testclient import TestClient

    from tests.conftest import alembic_config
    from alembic import command
    from app.main import create_app

    o, _ = old_install(tmp_path)
    s = settings.model_copy(update={"config_dir": settings.data_dir / "config-effective", "config_overrides": o})
    command.upgrade(alembic_config(s.db_url), "head")
    with TestClient(create_app(s)) as c:
        assert c.get("/health").status_code == 200
        note = json.loads((s.data_dir / "updates" / "config-update.json").read_text(encoding="utf-8"))
        assert note["message"].startswith("Config updated: 23 new actions") and note["notified"] is False
        assert (s.config_dir / "permissions.yaml").is_file()
        app = c.app
        pushed = []
        app.state.pusher.notify = lambda title, body, url="/": pushed.append(body)
        from app.housekeeping import Housekeeping
        hk = Housekeeping(app)
        hk._quiet = lambda: False
        hk.run_once()
        hk.run_once()
        assert len(pushed) == 1 and "23 new actions" in pushed[0]


# ---------------------------------------------------------------- the owner's edits in .md files ride along (owner 2026-10-14)

BASE = "<!-- kairo-personality v1 -->\n# Who you are\nYou are Kairo.\n\n# Style\n- Ask before acting.\n- Short answers.\n"
NEW = "<!-- kairo-personality v2 -->\n# Who you are\nYou are Kairo, chief of staff.\n\n# Style\n- Act, then say it in one line.\n- Short answers.\n"


def release_with(tmp: Path, *texts: str) -> Path:
    """A release whose personality.md history holds `texts` (oldest first; the last is current)."""
    rel = defaults_copy(tmp)
    h = history()
    h["text"]["personality.md"] = []
    h.setdefault("texts", {})["personality.md"] = {}
    for t in texts:
        cl.add_to_history(h, "personality.md", t.encode())
    (rel / cl.HISTORY).write_text(json.dumps(h), encoding="utf-8")
    (rel / "personality.md").write_text(texts[-1], encoding="utf-8")
    return rel


def test_owner_edits_are_carried_into_the_new_personality(tmp_path):
    o = fresh_overrides(tmp_path)
    mine = BASE.replace("- Short answers.\n", "- Short answers.\n- Call me D.\n")       # the owner's own line
    (o / "personality.md").write_text(mine.replace("\n", "\r\n"), encoding="utf-8", newline="")   # Notepad
    rel = release_with(tmp_path, BASE, NEW)
    out, report = build(tmp_path, o, rel)
    eff = (out / "personality.md").read_text(encoding="utf-8")
    assert "chief of staff" in eff and "Act, then say it" in eff and "Ask before acting" not in eff   # the release's changes
    assert "- Call me D." in eff                                                                     # the owner's edit
    assert eff.startswith("<!-- kairo-personality v2 -->")                                         # so: current
    assert report["merged"] == [{"file": "personality.md", "conflicts": 0}]
    assert (o / "personality.md").read_text(encoding="utf-8") == eff                                # the override is rebased
    assert (o / "personality.md.base").read_text(encoding="utf-8").strip() == cl.text_digest(NEW.encode())
    backups = list((tmp_path / "data" / "backups").glob("config-before-merge-*/personality.md"))
    assert len(backups) == 1 and "Call me D" in backups[0].read_text(encoding="utf-8")
    out, report = build(tmp_path, o, rel)                                                            # nothing new: untouched
    assert report["merged"] == [] and (out / "personality.md").read_text(encoding="utf-8") == eff
    assert not (out / "personality.md.base").exists()


def test_where_both_changed_the_same_line_the_owner_wins(tmp_path):
    o = fresh_overrides(tmp_path)
    (o / "personality.md").write_text(BASE.replace("- Ask before acting.", "- Never ask, just do it."), encoding="utf-8")
    (o / "personality.md.base").write_text(cl.text_digest(BASE.encode()), encoding="utf-8")
    out, report = build(tmp_path, o, release_with(tmp_path, BASE, NEW))
    eff = (out / "personality.md").read_text(encoding="utf-8")
    assert "- Never ask, just do it." in eff and "Act, then say it" not in eff and "chief of staff" in eff
    assert report["merged"] == [{"file": "personality.md", "conflicts": 1}]


def test_an_edit_next_to_a_release_change_keeps_both():
    """The owner added a line right below one the release reworded: no conflict, both apply."""
    base = "# Who\nYou are Kairo.\n- Ask first.\n"
    mine = "# Who\nYou are Kairo.\n- Ask first.\n- Call me D.\n"
    new = "<!-- kairo-personality v7 -->\n# Who\nYou are Kairo, chief.\n- Act.\n"
    merged, conflicts = cl.merge3(base, mine, new)
    assert merged == "<!-- kairo-personality v7 -->\n# Who\nYou are Kairo, chief.\n- Act.\n- Call me D.\n" and conflicts == 0


def test_merge3_basics():
    merged, conflicts = cl.merge3("a\nb\nc\n", "a\nB\nc\n", "a\nb\nc\nd\n")
    assert merged == "a\nB\nc\nd\n" and conflicts == 0
    merged, conflicts = cl.merge3("a\nb\nc\n", "a\nX\nc\n", "a\nY\nc\n")
    assert merged == "a\nX\nc\n" and conflicts == 1
