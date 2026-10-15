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
- anything else (.md): the owner's file, with the owner's edits carried into every new release
  text (3-way merge against the default it was edited from; where both changed the same lines,
  the owner's lines win). The override is rewritten as "new release + owner's edits" (backup
  first) and <name>.base records which default it is based on.

Migration (once, data/config without MARKER): the old full copies become overrides. A value
equal to any default the file ever had (defaults-history.json: fingerprints only, built from
git by scripts/config_history.py) is not an owner change and is dropped, so new defaults arrive;
everything else stays. A full backup goes to data/backups/config-before-overrides-<time>/.

profile.yaml (owner's decision 2026-10-15): the repo holds placeholders only. Its values are
never fingerprinted and never dropped as "a default". Once (PROFILE_PINNED), the profile Kairo
ran with until now is written into data/config/profile.yaml (only what differs from the new
defaults), so a release with placeholders never replaces the owner's real values.
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
PROFILE = "profile.yaml"
PROFILE_PINNED = ".profile-pinned-v1"
NO_HISTORY = {PROFILE}         # personal values: no fingerprints in the repo, never dropped as "a default"
NOT_CONFIG = {HISTORY, MARKER, README, PROFILE_PINNED}
BASE_SUFFIX = ".base"          # personality.md.base: digest of the default the owner's copy is based on

README_TEXT = """\
Kairo config: only what YOU changed.

Kairo's defaults come with every update (in the program folder, config/). This folder holds
only your changes; Kairo combines both at every start into data/config-effective (don't edit
that one, it is rebuilt).

- profile.yaml, schedule.yaml: write only the lines you want different, e.g.
      quiet_hours: { start: "22:30" }
- personality.md, voice.md: edit the file here; Kairo carries your edits into every new
  version by itself (your lines win where both changed). Don't edit the .base files.
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
    """A value's fingerprint. Only fingerprints go into the history (it is in the repo);
    profile.yaml is left out of it entirely (NO_HISTORY)."""
    canon = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:24]


def text_digest(raw: bytes) -> str:
    """Line endings and trailing blank space don't count (Notepad saves CRLF)."""
    text = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n").rstrip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalized(raw: bytes) -> str:
    return raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n").rstrip() + "\n"


def add_to_history(history: dict, name: str, raw: bytes) -> None:
    """Record one version of a default file (used by scripts/config_history.py). YAML: value
    fingerprints only (profile.yaml not at all: NO_HISTORY). Text files: their digests and full text (the
    base for carrying the owner's edits into a new version; they hold no personal values)."""
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
        history.setdefault("texts", {}).setdefault(name, {})[text_digest(raw)] = normalized(raw)


# ---------------------------------------------------------------- 3-way merge of text files

def _sync_regions(base: list, a: list, b: list) -> list[tuple]:
    import difflib
    am = difflib.SequenceMatcher(None, base, a, autojunk=False).get_matching_blocks()
    bm = difflib.SequenceMatcher(None, base, b, autojunk=False).get_matching_blocks()
    ia = ib = 0
    out = []
    while ia < len(am) and ib < len(bm):
        abase, amatch, alen = am[ia]
        bbase, bmatch, blen = bm[ib]
        lo, hi = max(abase, bbase), min(abase + alen, bbase + blen)
        if lo < hi:
            out.append((lo, hi, amatch + lo - abase, amatch + hi - abase, bmatch + lo - bbase, bmatch + hi - bbase))
        if abase + alen < bbase + blen:
            ia += 1
        else:
            ib += 1
    out.append((len(base), len(base), len(a), len(a), len(b), len(b)))
    return out


def _merge_close_edits(z: list, a: list, t: list) -> list | None:
    """Both sides changed this region. If their edits touch different base lines (e.g. the owner
    added a line right after one the release reworded), apply both; None if they really overlap."""
    import difflib

    def ops(other):
        return [(i1, i2, other[j1:j2]) for tag, i1, i2, j1, j2 in
                difflib.SequenceMatcher(None, z, other, autojunk=False).get_opcodes() if tag != "equal"]

    def overlap(x, y) -> bool:
        (i1, i2, xs), (k1, k2, ys) = x, y
        if i1 == i2 and k1 == k2:
            return i1 == k1 and xs != ys
        if i1 == i2:
            return k1 < i1 < k2
        if k1 == k2:
            return i1 < k1 < i2
        return i1 < k2 and k1 < i2

    oa, ot = ops(a), ops(t)
    if any(overlap(x, y) for x in oa for y in ot):
        return None
    out, pos = [], 0
    seen = set()
    for i1, i2, lines in sorted(oa + ot, key=lambda o: (o[0], o[1] - o[0])):
        key = (i1, i2, tuple(lines))
        if key in seen:                              # the same insertion on both sides: once
            continue
        seen.add(key)
        out += z[pos:i1] + lines
        pos = max(pos, i2)
    return out + z[pos:]


def merge3(base: str, mine: str, theirs: str) -> tuple[str, int]:
    """Line-based 3-way merge: changes from base to `mine` (the owner) and to `theirs` (the new
    release) both apply; where both changed the same lines, `mine` wins. -> (text, conflicts)"""
    b, a, t = base.splitlines(keepends=True), mine.splitlines(keepends=True), theirs.splitlines(keepends=True)
    out, conflicts, iz, ia, it = [], 0, 0, 0, 0
    for zs, ze, as_, ae, ts, te in _sync_regions(b, a, t):
        a_chunk, t_chunk, z_chunk = a[ia:as_], t[it:ts], b[iz:zs]
        if a_chunk or t_chunk or z_chunk:
            if a_chunk == t_chunk or t_chunk == z_chunk:
                out += a_chunk                       # same change, or only the owner changed it
            elif a_chunk == z_chunk:
                out += t_chunk                       # only the release changed it
            else:
                both = _merge_close_edits(z_chunk, a_chunk, t_chunk)
                if both is not None:
                    out += both                      # next to each other, not the same lines: both apply
                else:
                    out += a_chunk                   # the same lines changed on both sides: the owner's win
                    conflicts += 1
        out += b[zs:ze]
        iz, ia, it = ze, ae, te
    return "".join(out), conflicts


def _closest(history: dict, name: str, text: str) -> str | None:
    """The default (by digest) the owner's copy was most likely edited from."""
    import difflib
    best, score = None, -1.0
    for digest, old in (history.get("texts", {}).get(name) or {}).items():
        r = difflib.SequenceMatcher(None, old.splitlines(), text.splitlines(), autojunk=False).ratio()
        if r > score:
            best, score = digest, r
    return best


def _carry_edits(name: str, release_file: Path, mine_file: Path, history: dict, backup_root: Path) -> dict | None:
    """Rebase the owner's copy onto the release's text. Returns {"file", "conflicts"} if it changed."""
    release = normalized(release_file.read_bytes())
    mine = normalized(mine_file.read_bytes())
    sidecar = mine_file.with_name(mine_file.name + BASE_SUFFIX)
    texts = history.get("texts", {}).get(name) or {}
    base_digest = sidecar.read_text(encoding="utf-8").strip() if sidecar.is_file() else ""
    if base_digest not in texts:
        base_digest = _closest(history, name, mine) or ""
    release_digest = text_digest(release.encode())
    if base_digest == release_digest or base_digest not in texts:
        if not sidecar.is_file() and base_digest:
            sidecar.write_text(base_digest + "\n", encoding="utf-8")
        return None
    merged, conflicts = merge3(texts[base_digest], mine, release)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    keep = Path(backup_root) / f"config-before-merge-{stamp}"
    keep.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(mine_file, keep / name)
    mine_file.write_text(merged, encoding="utf-8")
    sidecar.write_text(release_digest + "\n", encoding="utf-8")
    return {"file": name, "conflicts": conflicts}


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
            known = {} if f.name in NO_HISTORY else history.get("yaml", {}).get(f.name, {})
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


# ---------------------------------------------------------------- the profile in use stays the owner's

_MISSING = object()


def _pin_profile(defaults: Path, overrides: Path, out: Path, backup_root: Path) -> bool:
    """Once: the profile values Kairo ran with (the last effective config) that differ from this
    release's defaults become the owner's. Returns True when something was written."""
    if (overrides / PROFILE_PINNED).is_file():
        return False
    kept = False
    in_use, release = out / PROFILE, defaults / PROFILE
    if in_use.is_file() and release.is_file():
        new_default = flatten(_load_yaml(release))
        mine_file = overrides / PROFILE
        mine = _load_yaml(mine_file)
        for path, value in flatten(_load_yaml(in_use)).items():
            if new_default.get(path, _MISSING) != value:
                _set(mine, path, value)
                kept = True
        if kept:
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = Path(backup_root) / f"config-before-profile-pin-{stamp}"
            backup.mkdir(parents=True, exist_ok=True)
            if mine_file.is_file():
                shutil.copyfile(mine_file, backup / PROFILE)
            shutil.copyfile(in_use, backup / f"in-use-{PROFILE}")
            header = f"# Your profile (kept from the version you ran on {stamp}; see README.txt).\n"
            mine_file.write_text(header + yaml.safe_dump(mine, sort_keys=False, allow_unicode=True), encoding="utf-8")
    (overrides / PROFILE_PINNED).write_text(dt.datetime.now().isoformat(timespec="seconds") + "\n", encoding="utf-8")
    return kept


# ---------------------------------------------------------------- build

def build(defaults: Path, overrides: Path, out: Path, *, backup_root: Path) -> dict:
    """Build `out` from the release defaults and the owner's overrides. Returns a report:
    {"migrated": bool, "new_actions": [...], "ignored": [...], "merged": [{"file", "conflicts"}],
     "profile_kept": bool}"""
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
    profile_kept = _pin_profile(defaults, overrides, out, backup_root)

    ignored: list[str] = []
    merged_files: list[dict] = []
    history = json.loads((defaults / HISTORY).read_text(encoding="utf-8")) if (defaults / HISTORY).is_file() else {}
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
            if mine.is_file():
                done = _carry_edits(f.name, f, mine, history, backup_root)
                if done:
                    merged_files.append(done)
            shutil.copyfile(mine if mine.is_file() else f, tmp / f.name)
    for f in sorted(overrides.iterdir()):
        if f.is_file() and f.name not in NOT_CONFIG and not f.name.endswith(BASE_SUFFIX) and not (tmp / f.name).exists():
            shutil.copyfile(f, tmp / f.name)            # the owner's own extra files
    if out.exists():
        shutil.rmtree(out)
    tmp.rename(out)

    now_actions = set((_load_yaml(out / PERMISSIONS).get("actions") or {}))
    new_actions = sorted(now_actions - previous_actions) if previous_actions is not None else []
    return {"migrated": migrated, "new_actions": new_actions, "ignored": ignored, "merged": merged_files,
            "profile_kept": profile_kept}
