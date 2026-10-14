#!/usr/bin/env python3
"""Write config/defaults-history.json: every value each default config file has ever had.

    python scripts/config_history.py

Run it after changing anything in config/ (a test fails until you do). Kairo needs it once per
install, to turn the full config copies of old installs into "only what the owner changed":
a value that was a default at some point was not set by the owner (server/app/config_layers.py).

Reads every version of config/* in git history plus the working tree.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from app.config_layers import HISTORY, NOT_CONFIG, add_to_history  # noqa: E402


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True).stdout


def main() -> int:
    history: dict = {"version": 1}
    names = sorted(n for n in {Path(p).name for p in git("log", "--format=", "--name-only", "--", "config/").decode().split()
                    if p.startswith("config/")} | {p.name for p in (ROOT / "config").iterdir() if p.is_file()}
                   if n not in NOT_CONFIG)
    for name in names:
        shas = git("log", "--reverse", "--format=%H", "--", f"config/{name}").decode().split()
        for sha in shas:
            try:
                add_to_history(history, name, git("show", f"{sha}:config/{name}"))
            except subprocess.CalledProcessError:
                pass                                   # deleted in that commit
        current = ROOT / "config" / name
        if current.is_file():
            add_to_history(history, name, current.read_bytes())
    (ROOT / "config" / HISTORY).write_text(json.dumps(history, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
                                           encoding="utf-8")
    print(f"wrote config/{HISTORY} ({len(names)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
