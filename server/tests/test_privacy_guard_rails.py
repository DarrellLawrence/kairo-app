"""The guard rails around scripts/privacy_scan.py (owner's master prompt 0.6): the git hooks, the CI privacy job,
the release script, .gitignore and the docs must stay wired up. The scanner's own behaviour is in test_privacy_scan.py."""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys

import pytest

from app.settings import REPO_ROOT
from tests.test_privacy_scan import HOOKS, NAME, SCRIPT, clean_env, git, make_repo, no_leak, write


def test_hooks_exist_are_executable_and_keep_lf_endings():
    for name in ("pre-commit", "commit-msg"):
        hook = HOOKS / name
        assert hook.is_file(), name
        assert b"\r" not in hook.read_bytes(), f"{name} must keep LF endings (a CRLF shebang does not run)"
        assert hook.read_bytes().startswith(b"#!/bin/sh")
    ls = subprocess.run(["git", "ls-files", "-s", "scripts/hooks"], cwd=REPO_ROOT, capture_output=True, text=True).stdout
    if ls:      # a source tree without .git (a zip of the repo) has no index to ask
        assert all(line.startswith("100755") for line in ls.splitlines()), ls
    attrs = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "scripts/hooks/*" in attrs and "eol=lf" in attrs


@pytest.mark.skipif(sys.platform == "win32", reason="hooks run in WSL/Linux; Git for Windows runs them through its own sh")
def test_the_hooks_stop_a_commit_with_a_term_and_let_a_clean_one_through(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "scripts").mkdir()
    shutil.copy2(SCRIPT, repo / "scripts" / "privacy_scan.py")
    shutil.copytree(HOOKS, repo / "scripts" / "hooks")
    for h in (repo / "scripts" / "hooks").iterdir():
        h.chmod(h.stat().st_mode | stat.S_IXUSR)
    git(repo, tmp_path, "config", "core.hooksPath", "scripts/hooks")
    env = clean_env(tmp_path, KAIRO_PERSONAL_TERMS=NAME)

    write(repo / "ok.txt", "hello\n")
    git(repo, tmp_path, "add", "ok.txt")
    ok = subprocess.run(["git", "commit", "-q", "-m", "clean"], cwd=repo, env=env, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr

    write(repo / "bad.txt", f"about {NAME}\n")
    git(repo, tmp_path, "add", "bad.txt")
    bad = subprocess.run(["git", "commit", "-q", "-m", "dirty"], cwd=repo, env=env, capture_output=True, text=True)
    assert bad.returncode != 0 and "bad.txt:1: personal term #1" in bad.stdout + bad.stderr
    no_leak(bad.stdout + bad.stderr, NAME)

    git(repo, tmp_path, "reset", "-q")
    write(repo / "msg.txt", "fine\n")
    git(repo, tmp_path, "add", "msg.txt")
    msg = subprocess.run(["git", "commit", "-q", "-m", f"met {NAME}"], cwd=repo, env=env, capture_output=True, text=True)
    assert msg.returncode != 0 and "personal term #1" in msg.stdout + msg.stderr      # the commit-msg hook
    no_leak(msg.stdout + msg.stderr, NAME)
