"""Render the voice latency harness's clips from a checkout:  python bench/voice/render.py [--force] [--only id,id]

The same as `python -m app.bench_corpus render` (run in server/), which is what a release uses: the clips
go to <data>/bench/corpus (KAIRO_DATA_DIR, else devdata/), never into the repo."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server"))

from app.bench_corpus import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["render", *sys.argv[1:]]))
