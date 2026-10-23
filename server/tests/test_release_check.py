"""The release check (scripts/check_release.py) catches a setup zip that leaves out code the
app needs at run time. Real case (diagnosis 2026-10-14): every release shipped without
server/mcp_kairo and server/hooks, so Kairo had no tools on kairo-home."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from app.settings import REPO_ROOT

spec = importlib.util.spec_from_file_location("check_release", REPO_ROOT / "scripts" / "check_release.py")
check_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_release)

SERVER = REPO_ROOT / "server"


def layout(tmp: Path, *, ship=("app", "mcp_kairo", "hooks", "migrations")) -> Path:
    app = tmp / "Kairo-Setup-x" / "app"
    server = app / "server"
    server.mkdir(parents=True)
    for name in ship:
        shutil.copytree(SERVER / name, server / name, ignore=shutil.ignore_patterns("__pycache__"))
    for f in ("alembic.ini", "requirements.txt"):
        shutil.copy(SERVER / f, server / f)
    (app / "web" / "dist").mkdir(parents=True)
    (app / "web" / "dist" / "index.html").write_text("<html></html>")
    shutil.copytree(REPO_ROOT / "config", app / "config")
    (app / "bench" / "voice").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "bench" / "voice" / "corpus.json", app / "bench" / "voice" / "corpus.json")
    (app / "release.json").write_text(json.dumps({"version": "x"}))
    return app


def test_first_party_names_cover_the_server_folder():
    names = check_release.first_party()
    assert {"app", "mcp_kairo", "hooks"} <= names and "tests" not in names


def test_complete_release_passes(tmp_path):
    assert check_release.check_app_dir(layout(tmp_path)) == []


@pytest.mark.parametrize("missing,expect", [
    ("mcp_kairo", "python -m mcp_kairo"),
    ("hooks", "PreToolUse hook"),
])
def test_missing_tool_bridge_fails(tmp_path, missing, expect):
    ship = tuple(n for n in ("app", "mcp_kairo", "hooks", "migrations") if n != missing)
    problems = check_release.check_app_dir(layout(tmp_path, ship=ship))
    assert any(expect in p for p in problems), problems


def test_import_of_unshipped_first_party_module_fails(tmp_path):
    app = layout(tmp_path)
    (app / "server" / "app" / "uses_ghost.py").write_text("import ghostpkg\n")
    problems = check_release.check_app_dir(app, names=check_release.first_party() | {"ghostpkg"})
    assert any("imports 'ghostpkg'" in p for p in problems)


def test_the_release_without_the_bench_corpus_fails_and_the_build_script_ships_it(tmp_path):
    app = layout(tmp_path)
    (app / "bench" / "voice" / "corpus.json").unlink()
    assert any("bench/voice/corpus.json" in p for p in check_release.check_app_dir(app))
    script = (REPO_ROOT / "scripts" / "build-release.sh").read_text()
    assert 'cp bench/voice/corpus.json "$root/app/bench/voice/"' in script


def test_build_script_ships_the_tool_bridge():
    script = (REPO_ROOT / "scripts" / "build-release.sh").read_text()
    copy = next(line for line in script.splitlines() if line.startswith("cp -r server/"))
    assert "server/mcp_kairo" in copy and "server/hooks" in copy
    assert "check_release.py" in script          # every build is checked before it can be published


@pytest.mark.parametrize("rel", ["server/tests/fakes/fake_claude.py", "server/faster_whisper/__init__.py.fake/x",
                                 "server/app/fake_whisper.py", "server/conftest.py"])
def test_test_code_and_stubs_never_ship(tmp_path, rel):
    app = layout(tmp_path)
    target = app / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# stub")
    problems = check_release.check_app_dir(app)
    if "faster_whisper" in rel:        # a stub folder next to app/ would shadow the real package
        assert any("unexpected server/faster_whisper" in p for p in problems), problems
    else:
        assert any("must not ship" in p or "unexpected" in p for p in problems), problems
