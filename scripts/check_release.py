#!/usr/bin/env python3
"""Fail the build if the setup zip is missing anything the code needs at run time.

    python scripts/check_release.py dist/Kairo-Setup-<version>.zip

Checks, on the extracted zip (not the repo):
1. Every first-party module that shipped code imports is in the zip
   (first-party = the packages and scripts next to app/ in the repo's server/).
2. What the shipped workspace.py writes into the CLI workspace exists in the zip:
   the PreToolUse hook script (.claude/settings.json) and the MCP server module
   (.mcp.json, `python -m mcp_kairo` with PYTHONPATH=server).
3. The rest of the layout the installer relies on (migrations, web build, config).
4. No tests, test stubs or fakes are shipped (a stub module could shadow a real package).

Standard library only. Exit 1 with one line per problem.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_SERVER = Path(__file__).resolve().parents[1] / "server"
NOT_SHIPPED = {"tests", "__pycache__", ".venv", ".pytest_cache"}
LAYOUT = ["server/alembic.ini", "server/migrations/env.py", "server/requirements.txt", "config/defaults-history.json",
          "web/dist/index.html", "config/permissions.yaml", "config/personality.md", "config/routing.yaml", "release.json",
          "bench/voice/corpus.json"]


def first_party(repo_server: Path = REPO_SERVER) -> set[str]:
    """Top-level names a shipped file could import from the server folder."""
    names = set()
    for p in repo_server.iterdir():
        if p.name in NOT_SHIPPED or p.name.startswith("."):
            continue
        if p.is_dir() and any(p.glob("*.py")):
            names.add(p.name)
        elif p.suffix == ".py":
            names.add(p.stem)
    return names


def imported(py: Path) -> set[str]:
    try:
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    except SyntaxError as exc:
        return {f"<syntax error: {exc}>"}
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module.split(".")[0])
    return out


def _exists_module(server: Path, name: str) -> bool:
    return (server / name / "__init__.py").is_file() or (server / name / "__main__.py").is_file() \
        or (server / f"{name}.py").is_file()


def check_app_dir(app_dir: Path, names: set[str] | None = None) -> list[str]:
    """app_dir = the zip's <name>/app folder (server/, web/, config/, release.json)."""
    names = first_party() if names is None else names
    server = app_dir / "server"
    problems = []
    for rel in LAYOUT:
        if not (app_dir / rel).exists():
            problems.append(f"missing {rel}")
    allowed = names | {"migrations", "alembic.ini"}
    for entry in sorted(server.iterdir()) if server.is_dir() else []:
        if entry.name not in allowed and not (entry.name.startswith("requirements") and entry.suffix == ".txt"):
            problems.append(f"unexpected server/{entry.name}: anything here can shadow a real package (e.g. a stub)")
    for f in sorted(app_dir.rglob("*")):
        parts = {x.lower() for x in f.relative_to(app_dir).parts}
        if parts & {"tests", "test", "fakes", "conftest.py"} or f.name.lower().startswith(("fake_", "test_")) and f.suffix == ".py":
            problems.append(f"test code must not ship: {f.relative_to(app_dir)}")
    for py in sorted(server.rglob("*.py")):
        for mod in sorted(imported(py) & names):
            if not _exists_module(server, mod):
                problems.append(f"{py.relative_to(app_dir)} imports '{mod}', which is not in the zip")
    ws = server / "app" / "workspace.py"
    if not ws.is_file():
        return problems + ["missing server/app/workspace.py"]
    spec = importlib.util.spec_from_file_location("shipped_workspace", ws)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)   # stdlib only; SERVER_DIR is the extracted server folder
    for entry in mod.settings_json()["hooks"]["PreToolUse"]:
        for hook in entry["hooks"]:
            for arg in hook.get("args", []):
                if arg.endswith(".py") and not Path(arg).is_file():
                    problems.append(f"the PreToolUse hook points to {arg}, which is not in the zip")
    for name, srv in mod.mcp_json("http://127.0.0.1:1/internal/tools", "x")["mcpServers"].items():
        args = srv.get("args", [])
        if "-m" in args:
            module = args[args.index("-m") + 1]
            root = Path(srv.get("env", {}).get("PYTHONPATH", server))
            if not _exists_module(root, module):
                problems.append(f".mcp.json starts '{name}' as python -m {module}, which is not in the zip")
    return problems


def check_zip(zip_path: Path) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(zip_path) as z:
        z.extractall(tmp)
        tops = [p for p in Path(tmp).iterdir() if p.is_dir()]
        if len(tops) != 1:
            return [f"expected one top folder in the zip, found {len(tops)}"]
        return check_app_dir(tops[0] / "app")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    problems = check_zip(Path(argv[0]))
    for p in problems:
        print(f"RELEASE CHECK: {p}", file=sys.stderr)
    if problems:
        return 1
    print("release check: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
