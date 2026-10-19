"""Backups (KICKOFF 15, Milestone D) and the restore test (acceptance: "Backup
restore: restore to temp -> DB opens, vault complete").

Nightly (03:30) a zip in <data>/backups/nightly/ with:
  db/kairo.sqlite   consistent copy made with SQLite's online backup API
  vault/            the Obsidian vault
  config/           permissions.yaml, profile.yaml, ...
  inbox/            captured files
  manifest.json     row counts per table + sha256 of every file
Secrets (Google tokens, keys) are NOT in the backup: after a restore you sign
in again with Kairo Logins. Kept: the newest 14 plus one per week for 8 weeks.

Every Sunday the newest backup is restored into a temp folder and checked:
the database opens, passes integrity_check, has the same row counts, and
every file is there with the same checksum.

Restore for real (Kairo stopped):  python -m app.backup restore <zip> --data <data dir>
The current database and vault are moved aside first, never deleted.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

INCLUDE = ("vault", "config", "inbox")
SIDE_FILES = ("-wal", "-shm", "-journal")   # SQLite's files next to the database
KEEP_DAILY = 14
KEEP_WEEKLY = 8


class BackupError(Exception):
    pass


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _counts(db: Path) -> dict[str, int]:
    con = sqlite3.connect(str(db))
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in sorted(tables)}
    finally:
        con.close()


def make(*, db_path: Path, data_dir: Path, config_dir: Path, dest_dir: Path, now: dt.datetime | None = None) -> Path:
    """Write one backup zip. Returns its path."""
    now = now or dt.datetime.now()
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not db_path.is_file():
        raise BackupError(f"no database at {db_path}")
    final = dest_dir / f"kairo-{now:%Y%m%d-%H%M%S}.zip"
    tmp_zip = final.with_suffix(".partial")
    with tempfile.TemporaryDirectory(prefix="kairo-backup-") as tmp:
        copy = Path(tmp) / "kairo.sqlite"
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(copy))
        try:
            src.backup(dst)   # consistent even while Kairo writes
        finally:
            dst.close()
            src.close()
        manifest = {"created": now.isoformat(), "db_rows": _counts(copy), "files": {}}
        sources = {"vault": data_dir / "vault", "config": config_dir, "inbox": data_dir / "inbox"}
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.write(copy, "db/kairo.sqlite")
            manifest["files"]["db/kairo.sqlite"] = _sha(copy)
            for name in INCLUDE:
                root = sources[name]
                if not root.is_dir():
                    continue
                for p in sorted(root.rglob("*")):
                    if p.is_file() and not p.name.endswith((".kairo-tmp", ".partial")) and ".stversions" not in p.parts:
                        arc = f"{name}/{p.relative_to(root).as_posix()}"
                        z.write(p, arc)
                        manifest["files"][arc] = _sha(p)
            z.writestr("manifest.json", json.dumps(manifest, indent=1))
    tmp_zip.replace(final)
    return final


def rotate(dest_dir: Path, *, keep_daily: int = KEEP_DAILY, keep_weekly: int = KEEP_WEEKLY) -> list[Path]:
    """Keep the newest `keep_daily` and the newest backup of each of the last `keep_weekly` weeks."""
    zips = sorted(dest_dir.glob("kairo-*.zip"), reverse=True)
    keep = set(zips[:keep_daily])
    weeks: dict[tuple, Path] = {}
    for z in zips:
        try:
            stamp = dt.datetime.strptime(z.stem.removeprefix("kairo-"), "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        weeks.setdefault(stamp.isocalendar()[:2], z)
    keep |= set(sorted(weeks.values(), reverse=True)[:keep_weekly])
    removed = [z for z in zips if z not in keep]
    for z in removed:
        z.unlink(missing_ok=True)
    return removed


def latest(dest_dir: Path) -> Path | None:
    zips = sorted(dest_dir.glob("kairo-*.zip"), reverse=True)
    return zips[0] if zips else None


def verify(zip_path: Path) -> dict:
    """Restore into a temp folder and check everything. Raises BackupError on any problem."""
    with tempfile.TemporaryDirectory(prefix="kairo-restore-test-") as tmp:
        root = Path(tmp)
        with zipfile.ZipFile(zip_path) as z:
            bad = z.testzip()
            if bad:
                raise BackupError(f"damaged file in the zip: {bad}")
            for name in z.namelist():
                target = (root / name).resolve()
                if root.resolve() not in target.parents:
                    raise BackupError(f"unsafe path in the zip: {name}")
            z.extractall(root)
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        db = root / "db" / "kairo.sqlite"
        con = sqlite3.connect(db)
        try:
            check = con.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            con.close()
        if check != "ok":
            raise BackupError(f"database integrity_check: {check}")
        counts = _counts(db)
        if counts != manifest["db_rows"]:
            raise BackupError("row counts differ from the manifest")
        missing = [f for f in manifest["files"] if not (root / f).is_file()]
        if missing:
            raise BackupError(f"{len(missing)} files missing, e.g. {missing[0]}")
        wrong = [f for f, h in manifest["files"].items() if _sha(root / f) != h]
        if wrong:
            raise BackupError(f"{len(wrong)} files changed, e.g. {wrong[0]}")
        vault_files = sum(1 for f in manifest["files"] if f.startswith("vault/"))
        return {"zip": zip_path.name, "created": manifest["created"], "tables": len(counts),
                "rows": sum(counts.values()), "vault_files": vault_files, "files": len(manifest["files"])}


def restore(zip_path: Path, data_dir: Path) -> Path:
    """Put a backup back (Kairo must be stopped). The current DB, vault and inbox
    are moved to <data>/restore-saved-<stamp>/ first. Returns that folder."""
    verify(zip_path)
    saved = data_dir / f"restore-saved-{dt.datetime.now():%Y%m%d-%H%M%S}"
    saved.mkdir(parents=True)
    # The database's own side files go with it: they hold its last writes (WAL mode, a forced stop), and
    # left next to the restored file they would be replayed into it.
    for rel in ("db/kairo.sqlite", *(f"db/kairo.sqlite{x}" for x in SIDE_FILES), "vault", "inbox"):
        cur = data_dir / rel
        if cur.exists():
            (saved / rel).parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(cur), str(saved / rel))
            except PermissionError as exc:
                raise BackupError(f"{rel} is in use. Stop Kairo first (admin PowerShell: Stop-ScheduledTask -TaskPath '\\Kairo\\' -TaskName 'Kairo'), then try again. "
                                  f"Anything already moved is in {saved}.") from exc
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if name.startswith(("db/", "vault/", "inbox/")):
                target = (data_dir / name).resolve()
                if data_dir.resolve() not in target.parents:
                    raise BackupError(f"unsafe path in the zip: {name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(name) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    return saved


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.backup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("restore", help="put a backup back (stop Kairo first)")
    r.add_argument("zip", type=Path)
    r.add_argument("--data", type=Path, required=True)
    v = sub.add_parser("verify", help="restore into a temp folder and check it")
    v.add_argument("zip", type=Path)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "verify":
            print(json.dumps(verify(a.zip), indent=1))
        else:
            saved = restore(a.zip, a.data)
            print(f"Restored {a.zip.name}. Your previous database and vault are in {saved}")
    except BackupError as exc:
        print(f"Backup problem: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
