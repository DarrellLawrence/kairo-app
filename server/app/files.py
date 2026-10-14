"""Files shared to Kairo (v8 block B). They live only on kairo-home, in vault/Files/<category>/
under their original name, with a short summary note next to them (<name>.md). The same file
(sha256) is filed once; the inbox copy is removed once it is filed. Files/ and anything over
20 MB never reach the iPhone: Kairo keeps a managed block in the vault's .stignore, so
Syncthing on kairo-home doesn't offer them to other devices.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

CATEGORIES = ["School", "Work", "Personal", "Money", "Receipts", "Health", "Home", "Other"]
SYNC_MAX_BYTES = 20 * 1024 * 1024
ROOT = "Files"
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f#^\[\]]+')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
STIGNORE_BEGIN = "// kairo: begin (managed by Kairo; your own lines go outside this block)"
STIGNORE_END = "// kairo: end"
LARGE_LIST = ".stignore-kairo-large"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def category(value) -> str:
    v = str(value or "").strip().capitalize()
    return v if v in CATEGORIES else "Other"


def safe_name(name: str | None, fallback: str) -> str:
    """The original name, made safe on Windows and Linux: no folders, no reserved names, no odd characters."""
    raw = str(name or "").replace("\\", "/").split("/")[-1]
    base = " ".join(_BAD.sub(" ", raw).split()).strip(" .")
    if not base or base.split(".")[0].lower() in _RESERVED:
        base = fallback
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    ext = ext[:10]
    stem = stem[:120 - len(ext) - 1] if ext else base[:120]
    return f"{stem}.{ext}" if ext else stem


def place(vault_dir: Path, cat: str, name: str, src: Path) -> str:
    """Move src into Files/<cat>/<name> (never overwrites: "name (2).ext"). Returns the vault-relative path."""
    folder = Path(vault_dir) / ROOT / category(cat)
    folder.mkdir(parents=True, exist_ok=True)
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    target, n = folder / name, 2
    while target.exists():
        target = folder / (f"{stem} ({n}).{ext}" if ext else f"{name} ({n})")
        n += 1
    shutil.move(str(src), str(target))
    return target.relative_to(vault_dir).as_posix()


def note_path(rel_file: str) -> str:
    return rel_file + ".md" if not rel_file.lower().endswith(".md") else rel_file[:-3] + " (summary).md"


def ensure_stignore(vault_dir: Path) -> list[str]:
    """Keep Files/ and every file over 20 MB off other devices. Returns the large files it lists."""
    vault_dir = Path(vault_dir)
    large = sorted("/" + p.relative_to(vault_dir).as_posix() for p in vault_dir.rglob("*")
                   if p.is_file() and not p.name.startswith(".stignore") and ROOT not in p.relative_to(vault_dir).parts[:1]
                   and p.stat().st_size > SYNC_MAX_BYTES)
    (vault_dir / LARGE_LIST).write_text("".join(f"{p}\n" for p in large), encoding="utf-8")
    # v8 C7: Obsidian's window layout is per device; synced, it only makes conflict copies.
    block = (f"{STIGNORE_BEGIN}\n/{ROOT}\n/.obsidian/workspace*.json\n/.obsidian/workspaces.json\n"
             f"#include {LARGE_LIST}\n{STIGNORE_END}")
    path = vault_dir / ".stignore"
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    pattern = re.compile(re.escape(STIGNORE_BEGIN) + r".*?" + re.escape(STIGNORE_END), re.S)
    new = pattern.sub(lambda _m: block, old, count=1) if pattern.search(old) else (old.rstrip() + "\n\n" + block + "\n").lstrip()
    if new != old:
        path.write_text(new, encoding="utf-8")
    return large
