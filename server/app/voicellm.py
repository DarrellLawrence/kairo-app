"""The local voice model: what the installer downloads, where it lives, which one talks.

Owner's decision 2026-10-16: voice is free and fast. A small LLM on the PC's RTX 3060, loaded day and
night, does the talking on calls and the voice button; Claude still does the work through ask_kairo
(brain/voice_local.py). The installer (as admin) downloads llama.cpp's server, the candidate models
(voicellm.json) and the turn detector into tools\\voicellm-<hash> (admins only; Kairo's account only
reads it). The voice lab (voicelab.py) measures the candidates on this PC and picks the fastest one
that sounds natural and calls the tool reliably. The speaker check's model (owner's approval 2026-10-20,
speaker.py) comes the same way, pinned in voicellm.json to one revision, size and sha256.

Downloads only from fixed hosts (outbound.py): GitHub's API and release files for llama.cpp, checked
against the sha256 GitHub publishes; Hugging Face for the models, checked against the sha256 Hugging
Face lists, at the revision recorded in installed.json. A file a complete older folder already has
(same place, same checksum) is copied from there instead of downloaded again.

  python -m app.voicellm download --to <folder>      the installer
  python -m app.voicellm kyutai-dir                  where the Kyutai lab copy belongs (installer)
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import json
import logging
import re
import shutil
import sys
import tarfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from sqlmodel import Session

from .db import SettingKV

log = logging.getLogger("kairo.voicellm")

MANIFEST = Path(__file__).with_name("voicellm.json")
INSTALLED = "installed.json"
COMPLETE = ".complete"
CHOICE_KEY = "voicellm.choice"          # {"model": id, "by": "lab" | "owner", "at": iso}
RESERVE_BYTES = 4 * 1024 ** 3           # left free on the disk after the downloads
HF = "https://huggingface.co"
GH_API = "https://api.github.com"
UA = {"User-Agent": "kairo-installer"}


def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def model_ids() -> list[str]:
    return [m["id"] for m in manifest()["models"]]


def spec(model_id: str) -> dict | None:
    return next((m for m in manifest()["models"] if m["id"] == model_id), None)


# ---------------------------------------------------------------- what is installed (read by Kairo)

def site(data_dir: Path) -> Path:
    from . import tool_sites
    return tool_sites.site("voicellm", data_dir)


def installed(site_dir: Path) -> dict:
    """installed.json of a complete site, else {}."""
    d = Path(site_dir)
    if not (d / COMPLETE).is_file():
        return {}
    try:
        return json.loads((d / INSTALLED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def server_binary(site_dir: Path) -> Path | None:
    rel = (installed(site_dir).get("llama") or {}).get("server")
    p = Path(site_dir) / rel if rel else None
    return p if p and p.is_file() else None


def model_path(site_dir: Path, model_id: str) -> Path | None:
    rel = ((installed(site_dir).get("models") or {}).get(model_id) or {}).get("file")
    p = Path(site_dir) / rel if rel else None
    return p if p and p.is_file() else None


def available(site_dir: Path) -> list[str]:
    """Installed candidate models, in the manifest's order."""
    return [m for m in model_ids() if model_path(site_dir, m)]


# g3.gemma_first (owner's decision, recipe 6): Gemma 4 E4B goes first in the pick order. voicellm.json is not touched
# (a new file = a new tools folder and a download); LFM2.5 2.6B, which speaks its reasoning, stays in the list.
GEMMA = "gemma-4-e4b"


def preferred(s: Session) -> str | None:
    """The model the owner wants tried first (None: the manifest's order)."""
    from . import flags
    return GEMMA if flags.on(s, "g3.gemma_first") else None


def in_pick_order(have: list[str], first: str | None) -> list[str]:
    """`have` with `first` (if it is there) moved to the front; the others keep their order."""
    return ([first] + [m for m in have if m != first]) if first in have else list(have)


def smart_turn_path(site_dir: Path) -> Path | None:
    rel = (installed(site_dir).get("smart_turn") or {}).get("file")
    p = Path(site_dir) / rel if rel else None
    return p if p and p.is_file() else None


def smart_turn_pin(cfg: dict | None = None) -> dict | None:
    """The turn detector's pin in voicellm.json (revision, path, size, sha256), None while the file still lists only a pattern."""
    cfg = manifest().get("smart_turn") if cfg is None else cfg
    return cfg if cfg and cfg.get("revision") and cfg.get("sha256") else None


def smart_turn_pinned_path(site_dir: Path) -> Path | None:
    """The downloaded turn-detector file, only when it is the pinned one (the installer recorded the revision and sha256 it
    checked; the size must still match). Anything else: None, and a call uses the copy inside pipecat."""
    pin, rec, path = smart_turn_pin(), installed(site_dir).get("smart_turn") or {}, smart_turn_path(site_dir)
    if pin and path and rec.get("sha256") == pin["sha256"] and rec.get("revision") == pin["revision"] \
            and path.stat().st_size == pin.get("size", path.stat().st_size):
        return path
    return None


def smart_turn_for_calls(s: Session, site_dir: Path) -> str | None:
    """g3.smart_turn_pin (read at the start of a call): the model file a WebRTC call loads. None: pipecat's own copy."""
    from . import flags
    if not flags.on(s, "g3.smart_turn_pin"):
        return None
    path = smart_turn_pinned_path(site_dir)
    return str(path) if path else None


def speaker_path(site_dir: Path) -> Path | None:
    """The speaker check's model (None: not installed, the check is off)."""
    rel = (installed(site_dir).get("speaker") or {}).get("file")
    p = Path(site_dir) / rel if rel else None
    return p if p and p.is_file() else None


def choice(s: Session) -> dict:
    row = s.get(SettingKV, CHOICE_KEY)
    try:
        return json.loads(row.value) if row and row.value else {}
    except ValueError:
        return {}


def set_choice(s: Session, model_id: str | None, by: str) -> dict:
    """model_id None (only the lab): no candidate passed, so the old voice answers until the owner picks one."""
    if model_id is None and by != "lab" or model_id is not None and model_id not in model_ids():
        raise ValueError(f"unknown voice model {model_id!r}")
    value = {"model": model_id, "by": by, "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    row = s.get(SettingKV, CHOICE_KEY) or SettingKV(key=CHOICE_KEY)
    row.value = json.dumps(value)
    s.add(row)
    s.commit()
    return value


def active(s: Session, site_dir: Path) -> str | None:
    """The model that talks: the lab's or the owner's pick if it's installed, else the first installed
    candidate (the smallest, fastest one is first in voicellm.json) until the lab has run. None when the
    lab ran and nothing passed (owner's decision 2026-10-16: then the old voice, until they pick one)."""
    have = in_pick_order(available(site_dir), preferred(s))
    c = choice(s)
    if lab_rejected(c):
        return None
    picked = c.get("model")
    if picked in have:
        return picked
    return have[0] if have else None


def lab_rejected(c: dict) -> bool:
    return "model" in c and c["model"] is None and c.get("by") == "lab"


def kyutai_dir(tools_dir: Path) -> Path:
    """tools\\kyutai-<hash of the kyutai section>: the same folder for the installer and Kairo."""
    k = manifest()["kyutai"]
    h = hashlib.sha256(json.dumps(k, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return Path(tools_dir) / f"kyutai-{h}"


def kyutai_ready(tools_dir: Path) -> bool:
    return (kyutai_dir(tools_dir) / COMPLETE).is_file()


# ---------------------------------------------------------------- downloads (the installer, as admin)

def _get_json(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={**UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:   # noqa: S310 (fixed https hosts)
        return json.loads(r.read().decode("utf-8"))


def _fetch(url: str, dest: Path, sha256: str, size: int | None = None) -> None:
    """Streams url to dest, checking its sha256 (and size). A wrong file is never kept."""
    if dest.is_file() and (size is None or dest.stat().st_size == size) and _sha256(dest) == sha256:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r, tmp.open("wb") as f:   # noqa: S310
        while chunk := r.read(1 << 20):
            h.update(chunk)
            f.write(chunk)
    if h.hexdigest() != sha256.lower():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{dest.name}: checksum does not match (expected {sha256[:12]}…)")
    tmp.replace(dest)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _older_sites(to: Path) -> list[Path]:
    """Complete voicellm folders next to this one (made from an earlier voicellm.json), newest first."""
    me = Path(to).resolve()
    found = [d for d in Path(to).parent.glob("voicellm-*") if d.is_dir() and d.resolve() != me and (d / COMPLETE).is_file()]
    return sorted(found, key=lambda d: d.stat().st_mtime, reverse=True)


def _get(url: str, to: Path, dest: Path, sha256: str, size: int | None, older: list[Path]) -> None:
    """_fetch, but a file an older folder already has at the same place is taken from there first (hard-linked,
    else copied: a new voicellm.json would otherwise download every model again); _fetch then checks its sha256."""
    rel = dest.relative_to(to)
    if not dest.is_file():
        for d in older:
            src = d / rel
            if src.is_file() and (size is None or src.stat().st_size == size) and _sha256(src) == sha256.lower():
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_name(dest.name + ".part")
                tmp.unlink(missing_ok=True)
                try:                  # a hard link: no second copy of a model of several GB on the disk
                    os.link(src, tmp)
                except OSError:       # another drive or a file system without links
                    shutil.copyfile(src, tmp)
                tmp.replace(dest)
                break
    _fetch(url, dest, sha256, size)


def _pinned(to: Path, cfg: dict, subdir: str, older: list[Path]) -> dict:
    """One pinned file (repo, revision, path, size and sha256 in voicellm.json): fetched by revision, checked, recorded."""
    dest = to / subdir / Path(cfg["path"]).name
    url = f"{HF}/{cfg['repo']}/resolve/{cfg['revision']}/{urllib.parse.quote(cfg['path'])}"
    _get(url, to, dest, cfg["sha256"], cfg.get("size"), older)
    return {"repo": cfg["repo"], "revision": cfg["revision"], "file": dest.relative_to(to).as_posix(),
            "sha256": cfg["sha256"], "size": cfg.get("size")}


def _speaker(to: Path, cfg: dict, older: list[Path]) -> dict:
    """The speaker check's model: one pinned file."""
    return _pinned(to, cfg, "speaker", older)


def pick_assets(assets: list[dict], patterns: list[str], tag: str) -> list[dict]:
    """One release asset per pattern ({tag} filled in). Fails when one is missing or has no digest."""
    out = []
    for pat in patterns:
        rx = re.compile(pat.replace("{tag}", re.escape(tag)))
        hits = sorted((a for a in assets if rx.search(a.get("name", ""))), key=lambda a: a["name"])
        if not hits:
            raise RuntimeError(f"llama.cpp {tag}: no release file matches {pat}")
        a = hits[-1]                                   # the newest CUDA 12.x build of that tag
        digest = str(a.get("digest") or "")
        if not digest.startswith("sha256:"):
            raise RuntimeError(f"llama.cpp {tag}: GitHub publishes no checksum for {a['name']}")
        out.append({"name": a["name"], "url": a["browser_download_url"], "sha256": digest.split(":", 1)[1],
                    "size": a.get("size")})
    return out


def _unpack(archive: Path, into: Path) -> None:
    into.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(into)                         # zipfile drops absolute paths and ".."
    else:
        with tarfile.open(archive) as t:
            t.extractall(into, filter="data")


def _llama(to: Path, cfg: dict, log_=print) -> dict:
    tag = cfg["tag"]
    rel = _get_json(f"{GH_API}/repos/{cfg['repo']}/releases/tags/{tag}")
    patterns = cfg["windows"] if sys.platform == "win32" else cfg["linux"]
    files = pick_assets(rel.get("assets") or [], patterns, tag)
    dl = to / "downloads"
    for f in files:
        log_(f"llama.cpp {tag}: {f['name']}")
        _fetch(f["url"], dl / f["name"], f["sha256"], f.get("size"))
        _unpack(dl / f["name"], to / "llama")
    exe = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    found = sorted((to / "llama").rglob(exe))
    if not found:
        raise RuntimeError(f"llama.cpp {tag}: {exe} is not in the release files")
    shutil.rmtree(dl, ignore_errors=True)
    return {"tag": tag, "server": found[0].relative_to(to).as_posix(), "files": [f["name"] for f in files]}


def hf_pick(repo: str, quants: list[str]) -> dict:
    """The GGUF file for the first quantization the repo has: {revision, path, sha256, size}."""
    info = _get_json(f"{HF}/api/models/{repo}")
    rev = info["sha"]
    tree = _get_json(f"{HF}/api/models/{repo}/tree/{rev}?recursive=true")
    ggufs = [f for f in tree if f.get("type") == "file" and f["path"].lower().endswith(".gguf")
             and "mmproj" not in f["path"].lower() and (f.get("lfs") or {}).get("oid")]
    for q in quants:
        rx = re.compile(rf"(?:^|[-_.]){re.escape(q)}(?:[-_.]|$)", re.I)
        hits = [f for f in ggufs if rx.search(Path(f["path"]).stem + ".")]
        if hits:
            f = sorted(hits, key=lambda x: len(x["path"]))[0]
            return {"revision": rev, "path": f["path"], "sha256": f["lfs"]["oid"], "size": f["lfs"].get("size") or f.get("size"),
                    "quant": q}
    raise RuntimeError(f"{repo}: no GGUF file for {', '.join(quants)}")


def _smart_turn(to: Path, cfg: dict, older: list[Path] = ()) -> dict:
    if smart_turn_pin(cfg):                            # pinned in voicellm.json: that file, nothing asked of the hub
        return _pinned(to, cfg, "smart-turn", list(older))
    info = _get_json(f"{HF}/api/models/{cfg['repo']}")
    rev = info["sha"]
    tree = _get_json(f"{HF}/api/models/{cfg['repo']}/tree/{rev}?recursive=true")
    rx = re.compile(cfg["pattern"])
    hits = sorted((f for f in tree if f.get("type") == "file" and rx.search(Path(f["path"]).name)
                   and (f.get("lfs") or {}).get("oid")), key=lambda f: f["path"])
    if not hits:
        raise RuntimeError(f"{cfg['repo']}: no turn-detector model file")
    f = hits[-1]                                       # the newest version's CPU build
    dest = to / "smart-turn" / Path(f["path"]).name
    _get(f"{HF}/{cfg['repo']}/resolve/{rev}/{f['path']}", to, dest, f["lfs"]["oid"], f["lfs"].get("size"), list(older))
    return {"repo": cfg["repo"], "revision": rev, "file": dest.relative_to(to).as_posix(), "sha256": f["lfs"]["oid"]}


def download(to: Path, *, log_=print, free_bytes=None) -> dict:
    """Everything voicellm.json lists, into `to`. Models smallest first; one that doesn't fit on
    the disk is skipped (and listed). Marks the folder complete at the end."""
    m = manifest()
    to = Path(to)
    to.mkdir(parents=True, exist_ok=True)
    older = _older_sites(to)
    out: dict = {"llama": _llama(to, m["llama_cpp"], log_), "models": {}, "skipped": [], "smart_turn": None,
                 "speaker": None, "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    picks = []
    for s in m["models"]:
        try:
            picks.append((s, hf_pick(s["repo"], s["quants"])))
        except Exception as exc:   # a candidate the lab then shows as "not installed", with why
            out["skipped"].append({"model": s["id"], "why": f"{type(exc).__name__}: {exc}"[:200]})
    for s, p in sorted(picks, key=lambda sp: sp[1]["size"] or 0):
        free = free_bytes() if free_bytes else shutil.disk_usage(to).free
        if (p["size"] or 0) + RESERVE_BYTES > free:
            out["skipped"].append({"model": s["id"], "why": f"not enough disk space ({free // 1024 ** 3} GB free)"})
            continue
        dest = to / "models" / s["id"] / Path(p["path"]).name
        log_(f"{s['title']}: {p['path']} ({(p['size'] or 0) / 1024 ** 3:.1f} GB)")
        _get(f"{HF}/{s['repo']}/resolve/{p['revision']}/{p['path']}", to, dest, p["sha256"], p["size"], older)
        out["models"][s["id"]] = {"file": dest.relative_to(to).as_posix(), "repo": s["repo"], "revision": p["revision"],
                                  "sha256": p["sha256"], "size": p["size"], "quant": p["quant"]}
    try:
        out["smart_turn"] = _smart_turn(to, m["smart_turn"], older)
    except Exception as exc:   # endpointing falls back to plain silence
        out["skipped"].append({"model": "smart-turn", "why": f"{type(exc).__name__}: {exc}"[:200]})
    try:
        out["speaker"] = _speaker(to, m["speaker"], older)
    except Exception as exc:   # the speaker check says "model missing" and stays off; calls still work
        out["skipped"].append({"model": "speaker", "why": f"{type(exc).__name__}: {exc}"[:200]})
    if not out["models"]:
        raise RuntimeError("no voice model could be installed: " + "; ".join(x["why"] for x in out["skipped"]))
    (to / INSTALLED).write_text(json.dumps(out, indent=1), encoding="utf-8")
    (to / COMPLETE).write_text("ok\n", encoding="ascii")
    return out


def main(argv: list[str]) -> int:
    if argv[:1] == ["download"] and "--to" in argv:
        out = download(Path(argv[argv.index("--to") + 1]))
        print(f"voice models ready: {', '.join(out['models'])}" + (f"; skipped {out['skipped']}" if out["skipped"] else ""))
        return 0
    if argv[:1] == ["kyutai-dir"] and "--tools" in argv:
        print(kyutai_dir(Path(argv[argv.index("--tools") + 1])))
        return 0
    print("usage: python -m app.voicellm download --to <folder> | kyutai-dir --tools <folder>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
