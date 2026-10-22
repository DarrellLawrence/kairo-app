"""The guard rails around scripts/privacy_scan.py (owner's master prompt 0.6): the git hooks, the CI privacy job,
the release script, .gitignore and the docs must stay wired up. The scanner's own behaviour is in test_privacy_scan.py."""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys

import pytest
import yaml

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


def workflow() -> dict:
    return yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def test_ci_has_a_privacy_job_with_full_history_the_terms_secret_and_a_pinned_gitleaks():
    job = workflow()["jobs"]["privacy"]
    steps = job["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["fetch-depth"] == 0
    text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "secrets.PERSONAL_TERMS" in text
    scan_step = next(s for s in steps if "privacy_scan.py" in s.get("run", ""))
    assert "--ci" in scan_step["run"] and "--history" in scan_step["run"]
    assert scan_step["env"]["KAIRO_PERSONAL_TERMS"] == "${{ secrets.PERSONAL_TERMS }}"
    gl = "\n".join(s.get("run", "") for s in steps) + str(job.get("env", ""))
    assert "gitleaks" in gl and "sha256sum -c" in gl                      # the download is verified
    assert any(len(str(v)) == 64 and set(str(v)) <= set("0123456789abcdef") for v in job.get("env", {}).values())
    assert "latest" not in gl.lower().replace("--log-opts", "")           # a pinned release, never "latest"


def test_ci_build_scans_the_release_zip_before_the_artifact_is_uploaded():
    steps = workflow()["jobs"]["build"]["steps"]
    names = [s.get("name", "") + s.get("uses", "") for s in steps]
    scan_i = next(i for i, s in enumerate(steps) if "privacy_scan.py" in s.get("run", "") and "--zip" in s.get("run", ""))
    upload_i = next(i for i, n in enumerate(names) if "upload-artifact" in n)
    assert scan_i < upload_i
    assert steps[scan_i]["env"]["KAIRO_PERSONAL_TERMS"] == "${{ secrets.PERSONAL_TERMS }}"


def test_existing_ci_jobs_are_still_there():
    jobs = set(workflow()["jobs"])
    assert {"build", "server", "voice", "voice-windows", "voice-install-windows", "memory", "browser", "release-tools",
            "web", "windows-install", "release", "privacy"} <= jobs


def test_build_release_scans_the_zip_and_removes_it_if_it_fails():
    sh = (REPO_ROOT / "scripts" / "build-release.sh").read_text(encoding="utf-8")
    assert "privacy_scan.py --zip" in sh
    assert sh.index("privacy_scan.py --zip") < sh.index('echo "$version" > "$out/VERSION"')
    assert 'rm -f "$zip"' in sh


def test_gitignore_keeps_personal_data_out():
    lines = {ln.strip() for ln in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    for must in ("private/", "data/", "devdata/", "*.local.*", ".env", "*.sqlite"):
        assert must in lines, must
    assert "!.env.example" in lines


def test_gitleaks_config_extends_the_defaults_and_every_exception_names_its_rule():
    import tomllib
    cfg = tomllib.loads((REPO_ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))
    assert cfg["extend"]["useDefault"] is True
    assert "allowlist" not in cfg                          # no exception that covers every rule
    for entry in cfg.get("allowlists", []):
        assert entry.get("targetRules"), entry.get("description")


def test_contributing_documents_the_hook_setup():
    for doc in ("README.md", "CONTRIBUTING.md"):
        text = (REPO_ROOT / doc).read_text(encoding="utf-8")
        assert "git config core.hooksPath scripts/hooks" in text, doc
