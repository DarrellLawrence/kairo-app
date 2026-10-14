"""Config in layers (fix P0.4, 2026-10-14).

    <release>/config/        the defaults; every update brings new keys and new actions
    data/config/             only what the owner changed (overrides)
    data/config-effective/   built from both at every start; what Kairo and the safety hook read

Before this, the installer copied config/ once at the first install and never again, so
kairo-home ran with the actions of its first release (23 of 38 missing).

Rules:
- permissions.yaml: the release decides which actions exist, the tiers, rules and default.
  The owner can only make things stricter: a higher tier, "deny" (the action is off), lower
  limits. Anything looser or unknown is ignored (and reported).
- other .yaml: deep merge, the owner's value wins (lists are replaced whole).
- anything else (.md): the owner's file replaces the release's whole.

Migration (once, data/config without MARKER): the old full copies become overrides. A value
equal to any default the file ever had (defaults-history.json: fingerprints only, built from
git by scripts/config_history.py) is not an owner change and is dropped, so new defaults arrive;
everything else stays. A full backup goes to data/backups/config-before-overrides-<time>/.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import yaml

HISTORY = "defaults-history.json"
MARKER = ".overrides-v1"
README = "README.txt"
PERMISSIONS = "permissions.yaml"
TIERS = ("0_read", "1_internal", "2_reversible", "3_outbound", "4_destructive", "5_money")
OFF = ("deny", "off", None)
LIMITS = ("approvals_open_max", "outbound_per_day_max")
NOT_CONFIG = {HISTORY, MARKER, README}

README_TEXT = """\
Kairo config: only what YOU changed.

Kairo's defaults come with every update (in the program folder, config/). This folder holds
only your changes; Kairo combines both at every start into data/config-effective (don't edit
that one, it is rebuilt).

- profile.yaml, schedule.yaml: write only the lines you want different, e.g.
      quiet_hours: { start: "22:30" }
- personality.md, voice.md: a file here replaces Kairo's whole file.
- permissions.yaml: you can only make things stricter:
      actions:
        gmail.label: 3_outbound     # ask me first
        gmail.trash: deny           # never
      limits:
        approvals_open_max: 5
  New actions come with updates. Looser settings are ignored.

Restart Kairo after a change (or wait for the next update).
"""


# ---------------------------------------------------------------- flattening and history

def flatten(data: Any, prefix: tuple = ()) -> dict[tuple, Any]:
    """Leaves of a YAML mapping by path. Lists are leaves (they are replaced whole)."""
    if isinstance(data, dict) and data:
        out: dict[tuple, Any] = {}
        for k, v in data.items():
            out.update(flatten(v, prefix + (k,)))
        return out
    return {prefix: data} if prefix else {}


def path_key(path: tuple) -> str:
    return json.dumps(list(path), ensure_ascii=False)


def value_key(value: Any) -> str:
    """A value's fingerprint. Only fingerprints go into the history: it is in the repo, and
    profile.yaml's values are personal (they live only in profile.yaml)."""
    canon = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:24]


def text_digest(raw: bytes) -> str:
    """Line endings and trailing blank space don't count (Notepad saves CRLF)."""
    text = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n").rstrip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def add_to_history(history: dict, name: str, raw: bytes) -> None:
    """Record one version of a default file (used by scripts/config_history.py)."""
    history.setdefault("version", 1)
    if name.endswith(".yaml"):
        data = yaml.safe_load(raw.decode("utf-8")) or {}
        paths = history.setdefault("yaml", {}).setdefault(name, {})
        for path, value in flatten(data).items():
            values = paths.setdefault(path_key(path), [])
            if value_key(value) not in values:
                values.append(value_key(value))
    else:
        digests = history.setdefault("text", {}).setdefault(name, [])
        if text_digest(raw) not in digests:
            digests.append(text_digest(raw))


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    return data if isinstance(data, dict) else {}


def _set(data: dict, path: tuple, value: Any) -> None:
    for k in path[:-1]:
        nxt = data.get(k)
        if not isinstance(nxt, dict):
            nxt = data[k] = {}
        data = nxt
    data[path[-1]] = value


# ---------------------------------------------------------------- merging

def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def merge_permissions(default: dict, over: dict) -> tuple[dict, list[str]]:
    """The release's permissions, made stricter by the owner where asked. Never looser."""
    out = copy.deepcopy(default)
    ignored: list[str] = []
    actions = out.setdefault("actions", {})
    for key in over:
        if key not in ("actions", "limits"):
            ignored.append(f"permissions {key}: only the release sets it")
    for action, tier in (over.get("actions") or {}).items():
        if action not in (default.get("actions") or {}):
            ignored.append(f"permissions action {action}: not an action of this release")
        elif tier in OFF or tier == "5_money":
            actions.pop(action, None)
        elif tier not in TIERS:
            ignored.append(f"permissions action {action}: {tier!r} is not a tier")
        elif TIERS.index(tier) < TIERS.index(actions[action]):
            ignored.append(f"permissions action {action}: {tier} is looser than the release's {actions[action]}")
        else:
            actions[action] = tier
    limits = out.setdefault("limits", {})
    for name, value in (over.get("limits") or {}).items():
        if name not in LIMITS or not isinstance(value, int) or isinstance(value, bool) or value < 0:
            ignored.append(f"permissions limit {name}: unknown or not a number")
        elif name in limits and value > limits[name]:
            ignored.append(f"permissions limit {name}: {value} is higher than the release's {limits[name]}")
        else:
            limits[name] = value
    return out, ignored


# ---------------------------------------------------------------- migration of the old full copies

def _migrate(overrides: Path, defaults: Path, backup_root: Path) -> bool:
    files = [f for f in overrides.iterdir() if f.is_file() and f.name not in NOT_CONFIG]
    if not files:
        return False
    history = json.loads((defaults / HISTORY).read_text(encoding="utf-8"))
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = Path(backup_root) / f"config-before-overrides-{stamp}"
    shutil.copytree(overrides, backup)
    for f in files:
        if not (defaults / f.name).is_file():
            continue                                    # the owner's own file: kept as is
        if f.suffix == ".yaml":
            known = history.get("yaml", {}).get(f.name, {})
            data = _load_yaml(f)
            if f.name == PERMISSIONS:
                data = {k: v for k, v in data.items() if k in ("actions", "limits")}
            delta: dict = {}
            for path, value in flatten(data).items():
                if value_key(value) not in known.get(path_key(path), []):
                    _set(delta, path, value)
            header = f"# Your changes only (moved here from the full copy on {stamp}; see README.txt).\n"
            f.write_text(header + (yaml.safe_dump(delta, sort_keys=False, allow_unicode=True) if delta else ""),
                         encoding="utf-8")
        elif text_digest(f.read_bytes()) in history.get("text", {}).get(f.name, []):
            f.unlink()                                  # an unchanged default: the release's applies
    return True


# ---------------------------------------------------------------- build

def build(defaults: Path, overrides: Path, out: Path, *, backup_root: Path) -> dict:
    """Build `out` from the release defaults and the owner's overrides. Returns a report:
    {"migrated": bool, "new_actions": [...], "ignored": [...]}"""
    defaults, overrides, out = Path(defaults), Path(overrides), Path(out)
    overrides.mkdir(parents=True, exist_ok=True)
    migrated = False
    previous_actions: set[str] | None = None
    if not (overrides / MARKER).is_file():
        if (overrides / PERMISSIONS).is_file():
            previous_actions = set((_load_yaml(overrides / PERMISSIONS).get("actions") or {}))
        migrated = _migrate(overrides, defaults, backup_root)
        (overrides / README).write_text(README_TEXT, encoding="utf-8")
        (overrides / MARKER).write_text(dt.datetime.now().isoformat(timespec="seconds") + "\n", encoding="utf-8")
    if previous_actions is None and (out / PERMISSIONS).is_file():
        previous_actions = set((_load_yaml(out / PERMISSIONS).get("actions") or {}))

    ignored: list[str] = []
    tmp = out.with_name(out.name + ".new")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    head = f"# Built by Kairo at start from {defaults} and {overrides}. Don't edit: change {overrides}.\n"
    for f in sorted(defaults.iterdir()):
        if not f.is_file() or f.name in NOT_CONFIG:
            continue
        mine = overrides / f.name
        if f.name == PERMISSIONS:
            merged, notes = merge_permissions(_load_yaml(f), _load_yaml(mine))
            ignored += notes
            (tmp / f.name).write_text(head + yaml.safe_dump(merged, sort_keys=False, allow_unicode=True), encoding="utf-8")
        elif f.suffix == ".yaml":
            merged = deep_merge(_load_yaml(f), _load_yaml(mine))
            (tmp / f.name).write_text(head + yaml.safe_dump(merged, sort_keys=False, allow_unicode=True), encoding="utf-8")
        else:
            shutil.copyfile(mine if mine.is_file() else f, tmp / f.name)
    for f in sorted(overrides.iterdir()):
        if f.is_file() and f.name not in NOT_CONFIG and not (tmp / f.name).exists():
            shutil.copyfile(f, tmp / f.name)            # the owner's own extra files
    if out.exists():
        shutil.rmtree(out)
    tmp.rename(out)

    now_actions = set((_load_yaml(out / PERMISSIONS).get("actions") or {}))
    new_actions = sorted(now_actions - previous_actions) if previous_actions is not None else []
    return {"migrated": migrated, "new_actions": new_actions, "ignored": ignored}
