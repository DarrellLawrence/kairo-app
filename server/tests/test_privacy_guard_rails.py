"""The guard rails around scripts/privacy_scan.py (owner's master prompt 0.6): the git hooks, the CI privacy jobs,
the release script, .gitignore and the docs must stay wired up. The scanner's own behaviour is in test_privacy_scan.py.
Every name here is made up (see test_privacy_scan.py)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys

import pytest
import yaml

from app import config_layers as cl
from app.settings import REPO_ROOT
from tests.test_config_layers import build as build_config
from tests.test_config_layers import fresh_overrides
from tests.test_privacy_scan import HOOKS, NAME, PLACE, SCRIPT, clean_env, commit, git, make_repo, no_leak, write

HOOK_FILES = ("pre-commit", "pre-merge-commit", "commit-msg", "prepare-commit-msg", "_common.sh")
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="hooks run in WSL/Linux; Git for Windows runs them through its own sh")


def test_hooks_exist_are_executable_and_keep_lf_endings():
    for name in HOOK_FILES:
        hook = HOOKS / name
        assert hook.is_file(), name
        assert b"\r" not in hook.read_bytes(), f"{name} must keep LF endings (a CRLF shebang does not run)"
    for name in ("pre-commit", "pre-merge-commit", "commit-msg", "prepare-commit-msg"):
        assert (HOOKS / name).read_bytes().startswith(b"#!/bin/sh")
    ls = subprocess.run(["git", "ls-files", "-s", "scripts/hooks"], cwd=REPO_ROOT, capture_output=True, text=True).stdout
    if ls:      # a source tree without .git (a zip of the repo) has no index to ask
        assert all(line.startswith("100755") for line in ls.splitlines()), ls
        assert len(ls.splitlines()) >= len(HOOK_FILES)
    attrs = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "scripts/hooks/*" in attrs and "eol=lf" in attrs


def test_pre_commit_shows_what_gitleaks_found_and_looks_into_archives():
    text = (HOOKS / "pre-commit").read_text(encoding="utf-8")
    assert "--verbose" in text and "--redact" in text                  # the developer sees file, line and rule (only 'leaks found: 2' before)
    assert "max-archive-depth" in text
    assert '"$py" "$scan" --staged' in text                            # found through _common.sh: this checkout's, else the installed copy


def hooked_repo(tmp_path, name: str = "repo"):
    """A repo whose hooks are the real ones (core.hooksPath=scripts/hooks) and whose scanner is the real one."""
    repo = make_repo(tmp_path, name)
    (repo / "scripts").mkdir()
    shutil.copy2(SCRIPT, repo / "scripts" / "privacy_scan.py")
    shutil.copytree(HOOKS, repo / "scripts" / "hooks")
    for h in (repo / "scripts" / "hooks").iterdir():
        h.chmod(h.stat().st_mode | stat.S_IXUSR)
    git(repo, tmp_path, "config", "core.hooksPath", "scripts/hooks")
    return repo


def try_commit(repo, tmp_path, *args, terms=(NAME, PLACE), **env_extra):
    env = clean_env(tmp_path, KAIRO_PERSONAL_TERMS="\n".join(terms), **env_extra)
    p = subprocess.run(["git", "commit", "-q", *args], cwd=repo, env=env, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


@posix_only
def test_the_hooks_stop_a_commit_with_a_term_and_let_a_clean_one_through(tmp_path):
    repo = hooked_repo(tmp_path)
    write(repo / "ok.txt", "hello\n")
    git(repo, tmp_path, "add", "ok.txt")
    code, out = try_commit(repo, tmp_path, "-m", "clean")
    assert code == 0, out

    write(repo / "bad.txt", f"about {NAME}\n")
    git(repo, tmp_path, "add", "bad.txt")
    code, out = try_commit(repo, tmp_path, "-m", "dirty")
    assert code != 0 and "bad.txt:1: personal term #1" in out
    no_leak(out, NAME)

    git(repo, tmp_path, "reset", "-q")
    write(repo / "msg.txt", "fine\n")
    git(repo, tmp_path, "add", "msg.txt")
    code, out = try_commit(repo, tmp_path, "-m", f"met {NAME}")
    assert code != 0 and "personal term #1" in out                       # the commit-msg hook
    no_leak(out, NAME)


@posix_only
@pytest.mark.parametrize("how", ["dash-m-hash-first", "dash-m-second-paragraph", "dash-F-file", "other-comment-char"])
def test_a_line_that_starts_with_a_hash_in_the_message_is_scanned(tmp_path, how):
    """git keeps '#' lines unless it runs in 'strip' mode, and -m and -F do not strip. The hook used to drop them."""
    repo = hooked_repo(tmp_path)
    write(repo / "ok.txt", "hello\n")
    git(repo, tmp_path, "add", "ok.txt")
    if how == "dash-m-hash-first":
        args = ("-m", f"#97 {NAME} asked for this")
    elif how == "dash-m-second-paragraph":
        args = ("-m", "Fix the import", "-m", f"# visited {NAME}")
    elif how == "dash-F-file":
        write(tmp_path / "msg.txt", f"Fix the import\n\n# {NAME} again\n")
        args = ("-F", str(tmp_path / "msg.txt"))
    else:
        git(repo, tmp_path, "config", "core.commentChar", ";")
        args = ("-m", "s", "-m", f"#{NAME}")
    code, out = try_commit(repo, tmp_path, *args)
    assert code != 0 and "personal term #1" in out, out
    no_leak(out, NAME)
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo, env=clean_env(tmp_path), capture_output=True, text=True)
    assert log.returncode != 0 or NAME not in log.stdout           # nothing was committed


SCISSORS = "-" * 24 + " >8 " + "-" * 24             # git's cut line comes after its comment char and a space


def try_git(repo, tmp_path, *args, terms=(NAME, PLACE), **env_extra):
    env = clean_env(tmp_path, KAIRO_PERSONAL_TERMS="\n".join(terms), **env_extra)
    p = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


@posix_only
@pytest.mark.parametrize("how", ["dash-m-scissors", "dash-F-scissors", "dash-F-scissors-other-comment-char",
                                 "merge-dash-m", "merge-dash-m-token"])
def test_text_that_git_keeps_is_scanned_even_below_a_scissors_line_or_in_a_merge(tmp_path, how):
    """git cuts at a scissors line only when an editor runs with -v or --cleanup=scissors, and `git merge -m` keeps
    '#' lines like `git commit -m` does. The hook used to drop both, so the text went into the history unscanned."""
    from tests.test_privacy_scan import GH_PAT
    repo = hooked_repo(tmp_path)
    commit(repo, tmp_path, "start", {"a.txt": "x\n"})
    if how.startswith("merge"):
        git(repo, tmp_path, "switch", "-q", "-c", "side")
        commit(repo, tmp_path, "side", {"s.txt": "y\n"})
        git(repo, tmp_path, "switch", "-q", "main")
        last = f"# {GH_PAT}" if how.endswith("token") else f"# {NAME}"
        code, out = try_git(repo, tmp_path, "merge", "--no-ff", "-q", "-m", f"Merge side\n\n#97 follow-up\n{last}", "side")
        assert code != 0 and ("personal term #1" in out if "token" not in how else "secret: GitHub token" in out), out
        no_leak(out, NAME, GH_PAT)
        assert git(repo, tmp_path, "log", "-1", "--format=%s").strip() == "start"          # nothing was merged
        return
    write(repo / "ok.txt", "hello\n")
    git(repo, tmp_path, "add", "ok.txt")
    char = ";" if how.endswith("other-comment-char") else "#"
    if char != "#":
        git(repo, tmp_path, "config", "core.commentChar", char)
    message = f"Fix the import\n\n{char} {SCISSORS}\n{NAME} asked for this\n"
    if how == "dash-m-scissors":
        args = ("-m", message)
    else:
        write(tmp_path / "msg.txt", message)
        args = ("-F", str(tmp_path / "msg.txt"))
    code, out = try_commit(repo, tmp_path, *args)
    assert code != 0 and "commit message:4: personal term #1" in out, out
    no_leak(out, NAME)


EDITOR = """#!/bin/sh
# a fake editor: a subject line is typed above whatever git put in the file
{ printf '%s\\n' "$KAIRO_TEST_SUBJECT"; cat "$1"; } > "$1.new" && mv "$1.new" "$1"
"""
EDITOR_GLUED = """#!/bin/sh
# a fake editor: the typed lines replace the empty line git put above its comment block
{ printf '%s\\n' "$KAIRO_TEST_SUBJECT"; tail -n +2 "$1"; } > "$1.new" && mv "$1.new" "$1"
"""
EDITOR_REPLACE = """#!/bin/sh
# a fake editor: the whole file is replaced with what was typed
printf '%s\\n' "$KAIRO_TEST_SUBJECT" > "$1"
"""


@posix_only
@pytest.mark.parametrize("how", ["hash-line-glued-to-the-template-whitespace-cleanup", "last-line-starts-with-at"])
def test_an_editor_commit_scans_every_line_that_was_typed(tmp_path, how):
    """Only the lines git itself wrote into the file are left out, not every trailing line that starts with a sign."""
    repo = hooked_repo(tmp_path)
    write(repo / "ok.txt", "x\n")
    git(repo, tmp_path, "add", "ok.txt")
    if how.startswith("hash"):
        editor, args, typed = EDITOR_GLUED, ("--cleanup=whitespace",), f"Fix the import\n\n# {NAME}"
    else:
        editor, args, typed = EDITOR_REPLACE, (), f"subject line\nbody text\n@{NAME} asked for this"
    ed = write(tmp_path / "editor.sh", editor)
    ed.chmod(0o755)
    code, out = try_commit(repo, tmp_path, *args, GIT_EDITOR=str(ed), KAIRO_TEST_SUBJECT=typed)
    assert code != 0 and "personal term #1" in out, out
    no_leak(out, NAME)
    log = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=repo, env=clean_env(tmp_path), capture_output=True, text=True)
    assert NAME not in log.stdout


@posix_only
def test_an_editor_commit_is_not_blocked_by_gits_own_comment_block(tmp_path):
    """The template lists the files of the commit: a deleted file named after a place would block the very commit that
    removes it, which teaches people to use --no-verify."""
    repo = hooked_repo(tmp_path)
    commit(repo, tmp_path, "start", {f"{PLACE.lower()}-notes.txt": "harmless\n", "keep.txt": "x\n"})
    git(repo, tmp_path, "rm", "-q", f"{PLACE.lower()}-notes.txt")
    editor = write(tmp_path / "editor.sh", EDITOR)
    editor.chmod(0o755)
    code, out = try_commit(repo, tmp_path, GIT_EDITOR=str(editor), KAIRO_TEST_SUBJECT="Remove the notes")
    assert code == 0, out
    assert git(repo, tmp_path, "log", "-1", "--format=%s").strip() == "Remove the notes"
    assert not (repo / ".git" / "kairo-msg-prepared").exists()                     # the copy for commit-msg is cleaned up


@posix_only
def test_an_editor_commit_still_scans_a_line_typed_above_the_template(tmp_path):
    repo = hooked_repo(tmp_path)
    write(repo / "ok.txt", "x\n")
    git(repo, tmp_path, "add", "ok.txt")
    editor = write(tmp_path / "editor.sh", EDITOR)
    editor.chmod(0o755)
    code, out = try_commit(repo, tmp_path, GIT_EDITOR=str(editor), KAIRO_TEST_SUBJECT=f"#12 {NAME}")
    assert code != 0 and "personal term #1" in out
    no_leak(out, NAME)


@posix_only
def test_commit_dash_v_can_remove_a_term_the_diff_below_the_scissors_line_is_not_the_message(tmp_path):
    repo = hooked_repo(tmp_path)
    write(repo / "b.txt", f"keep\n{NAME}\n")
    git(repo, tmp_path, "add", "b.txt")
    code, out = try_commit(repo, tmp_path, "--no-verify", "-m", "oops")
    assert code == 0, out
    write(repo / "b.txt", "keep\n")
    git(repo, tmp_path, "add", "b.txt")
    editor = write(tmp_path / "editor.sh", EDITOR)
    editor.chmod(0o755)
    code, out = try_commit(repo, tmp_path, "-v", GIT_EDITOR=str(editor), KAIRO_TEST_SUBJECT="Remove personal data")
    assert code == 0, out
    assert git(repo, tmp_path, "log", "-1", "--format=%s").strip() == "Remove personal data"


@posix_only
def test_message_scanning_unit_cases(tmp_path):
    from tests.test_privacy_scan import scan
    repo = make_repo(tmp_path)
    template = "\n# Please enter the commit message for your changes. Lines starting\n# with '#' will be ignored.\n#\n# On branch main\n#\tdeleted:    " \
               + PLACE.lower() + "-notes.txt\n"
    prepared = write(tmp_path / "prepared.txt", template)

    def code(message, *extra, terms=(NAME, PLACE)):
        f = write(tmp_path / "m.txt", message)
        return scan(tmp_path, "--message-file", str(f), *extra, terms=list(terms), cwd=repo)[0]

    assert code(f"Subject\n{template}") == 1                                                # no editor (-m, -F): every line
    assert code(f"Subject\n{template}", "--message-prepared", str(prepared)) == 0          # git's own block, unchanged
    assert code(f"Subject\n\n# note: {NAME}{template}", "--message-prepared", str(prepared)) == 1   # a typed '#' line
    assert code(f"Subject\n{template}# {NAME}\n", "--message-prepared", str(prepared)) == 1         # typed below the block
    assert code(f"Subject\n\n# {SCISSORS}\n-{NAME}\n") == 1                                 # -m/-F: git keeps text below it
    diff = f"# {SCISSORS}\n# Do not modify or remove the line above.\n# Everything below it will be ignored.\ndiff --git a/b b/b\n-{NAME}\n"
    write(prepared, template + diff)                                                       # commit -v: git cuts at its line
    assert code(f"Remove it\n{template}{diff}", "--message-prepared", str(prepared)) == 0
    fake = f"Subject\n# {SCISSORS}\n{NAME} was here\n"                                     # a cut line from -e -F, no -v:
    write(prepared, fake + template)                                                       # git keeps the text below it
    assert code(fake + template, "--message-prepared", str(prepared)) == 1
    write(prepared, template)
    git(repo, tmp_path, "config", "commit.cleanup", "whitespace")                         # git keeps its comment block then
    assert code(f"Subject\n{template}", "--message-prepared", str(prepared)) == 1
    git(repo, tmp_path, "config", "--unset", "commit.cleanup")
    git(repo, tmp_path, "config", "core.commentChar", ";")
    write(prepared, template.replace("#", ";"))
    assert code(f"Subject\n{template.replace('#', ';')}", "--message-prepared", str(prepared)) == 0
    assert code(f"Subject\n# {NAME}\n{template.replace('#', ';')}", "--message-prepared", str(prepared)) == 1
    f = write(tmp_path / "m.txt", "Subject\n")                                             # hooks older than the scanner
    rc, out = scan(tmp_path, "--message-file", str(f), "--message-editor", terms=[NAME], cwd=repo)
    assert rc == 2 and "--install-hooks" in out


@posix_only
def test_the_commit_that_ends_a_merge_is_scanned_too(tmp_path):
    """git runs pre-merge-commit, not pre-commit, for it (and commit-msg for its message)."""
    repo = hooked_repo(tmp_path)
    commit(repo, tmp_path, "start", {"a.txt": "x\n"})
    git(repo, tmp_path, "switch", "-q", "-c", "side")
    commit(repo, tmp_path, "side", {"s.txt": "y\n"})
    git(repo, tmp_path, "switch", "-q", "main")
    commit(repo, tmp_path, "main", {"m.txt": "z\n"})
    env = clean_env(tmp_path, KAIRO_PERSONAL_TERMS=NAME)
    p = subprocess.run(["git", "merge", "--no-ff", "--no-commit", "side"], cwd=repo, env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    write(repo / "resolved.txt", f"fixed by {NAME}\n")
    git(repo, tmp_path, "add", "resolved.txt")
    code, out = try_commit(repo, tmp_path, "-m", "Merge side")
    assert code != 0 and "resolved.txt:1: personal term #1" in out, out
    no_leak(out, NAME)
    git(repo, tmp_path, "rm", "-q", "-f", "resolved.txt")
    code, out = try_commit(repo, tmp_path, "-m", f"Merge side for {NAME}")
    assert code != 0 and "personal term #1" in out, out                                     # the message of the merge
    code, out = try_commit(repo, tmp_path, "-m", "Merge side")
    assert code == 0, out


@posix_only
def test_install_hooks_protects_old_branches_and_every_worktree(tmp_path):
    """core.hooksPath=scripts/hooks is relative: on a branch that predates the hooks git finds no hook and commits
    unscanned. The installed hooks (and a copy of the scanner) live in the shared git folder."""
    repo = make_repo(tmp_path)
    commit(repo, tmp_path, "an old commit", {"README.md": "old\n"})
    git(repo, tmp_path, "branch", "old")
    git(repo, tmp_path, "branch", "old2")
    (repo / "scripts").mkdir()
    shutil.copy2(SCRIPT, repo / "scripts" / "privacy_scan.py")
    shutil.copytree(HOOKS, repo / "scripts" / "hooks")
    commit(repo, tmp_path, "guard rails")
    env = clean_env(tmp_path)
    p = subprocess.run([sys.executable, str(repo / "scripts" / "privacy_scan.py"), "--install-hooks"], cwd=repo, env=env,
                       capture_output=True, text=True)
    assert p.returncode == 0 and "hooks installed" in p.stdout, p.stdout + p.stderr
    hooks_dir = git(repo, tmp_path, "config", "--get", "core.hooksPath").strip()
    assert os.path.isabs(hooks_dir) and (repo / ".git" / "kairo-hooks" / "privacy_scan.py").is_file()

    git(repo, tmp_path, "switch", "-q", "old")
    assert not (repo / "scripts").exists()                                      # this branch has no scanner and no hooks
    write(repo / "bad.txt", f"about {NAME}\n")
    git(repo, tmp_path, "add", "bad.txt")
    code, out = try_commit(repo, tmp_path, "-m", "on the old branch")
    assert code != 0 and "bad.txt:1: personal term #1" in out, out
    git(repo, tmp_path, "reset", "-q")
    write(repo / "good.txt", "fine\n")
    git(repo, tmp_path, "add", "good.txt")
    code, out = try_commit(repo, tmp_path, "-m", "clean on the old branch")
    assert code == 0, out

    wt = tmp_path / "second-worktree"
    git(repo, tmp_path, "worktree", "add", "-q", str(wt), "old2")
    write(wt / "bad.txt", f"about {PLACE}\n")
    git(wt, tmp_path, "add", "bad.txt")
    code, out = try_commit(wt, tmp_path, "-m", "in the worktree")
    assert code != 0 and "personal term #2" in out, out


@posix_only
def test_the_hooks_fail_closed_when_there_is_no_scanner_to_run(tmp_path):
    repo = hooked_repo(tmp_path)
    (repo / "scripts" / "privacy_scan.py").unlink()
    write(repo / "a.txt", "fine\n")
    git(repo, tmp_path, "add", "a.txt")
    code, out = try_commit(repo, tmp_path, "-m", "no scanner")
    assert code != 0 and "scripts/privacy_scan.py is missing" in out and "--no-verify" in out


# ---------------------------------------------------------------- CI

def workflow() -> dict:
    return yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def workflow_text() -> str:
    return (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def run_steps(job: dict) -> list[str]:
    return [s.get("run", "") for s in job["steps"]]


def test_ci_privacy_job_scans_the_history_with_a_pinned_checksummed_gitleaks_and_no_verbose():
    job = workflow()["jobs"]["privacy"]
    steps = job["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["fetch-depth"] == 0 and checkout["with"]["persist-credentials"] is False
    assert job["permissions"] == {"contents": "read"}
    whole = next(s for s in steps if s.get("run", "").strip() == 'python "$SCANNER" --ci --history')
    assert whole["env"]["KAIRO_PERSONAL_TERMS"] == "${{ secrets.PERSONAL_TERMS }}"
    gl = "\n".join(run_steps(job)) + str(job.get("env", ""))
    assert "gitleaks" in gl and "sha256sum -c" in gl                      # the download is verified
    assert any(len(str(v)) == 64 and set(str(v)) <= set("0123456789abcdef") for v in job.get("env", {}).values())
    assert "latest" not in gl.lower().replace("--log-opts", "")           # a pinned release, never "latest"
    leak_step = next(r for r in run_steps(job) if "gitleaks/gitleaks" in r and " git " in r)
    assert "--verbose" not in leak_step and " -v " not in leak_step       # --verbose prints the matched line, the file and the author
    assert "--max-archive-depth" in leak_step                             # a tarball is no hiding place
    assert "--gitleaks-report" in leak_step and "exit $rc" in leak_step   # printed by privacy_scan (no matched text); gitleaks' exit code decides
    assert "--verbose" not in yaml.safe_dump(job)                         # (the comment that says why is not part of the job)


def test_a_pull_request_is_judged_by_the_base_branchs_scanner_and_gitleaks_config():
    for name in ("privacy", "privacy-zip"):
        job = workflow()["jobs"][name]
        choose = next(r for r in run_steps(job) if "origin/${BASE_REF}:scripts/privacy_scan.py" in r)
        assert 'git show "origin/${BASE_REF}:.gitleaks.toml"' in choose and "pull_request" in choose
        assert "SCANNER=" in choose and "GITLEAKS_CONFIG=" in choose
        scans = [r for r in run_steps(job) if '"$SCANNER"' in r]
        assert len(scans) >= 2, scans
        others = [r for r in run_steps(job) if "scripts/privacy_scan.py" in r and r is not choose]
        assert others == [], others                                                # never `python scripts/privacy_scan.py`
        for r in run_steps(job):
            if "gitleaks/gitleaks" in r and "--config" in r:
                assert '--config "$GITLEAKS_CONFIG"' in r
    text = workflow_text()
    assert "pull_request_target" not in text                                   # forks never get the secret or a write token


def test_gitleaks_never_reads_an_ignore_file_and_no_job_gets_a_write_token_it_does_not_need():
    """A .gitleaksignore in the scanned folder silences gitleaks whatever --config says (the base branch's scanner also
    flags the file in a pull request). Only the release job may write; the build job runs npm scripts."""
    wf = workflow()
    for name in ("privacy", "privacy-zip"):
        gl = next(r for r in run_steps(wf["jobs"][name]) if "gitleaks/gitleaks" in r and "--config" in r)
        assert "rm -f" in gl and ".gitleaksignore" in gl and gl.index(".gitleaksignore") < gl.index("gitleaks/gitleaks"), name
    assert wf["permissions"] == {"contents": "read"}
    writers = {n for n, job in wf["jobs"].items() if "write" in str(job.get("permissions", ""))}
    assert writers == {"release"}, writers
    build_checkout = next(s for s in wf["jobs"]["build"]["steps"] if str(s.get("uses", "")).startswith("actions/checkout"))
    assert build_checkout["with"]["persist-credentials"] is False


def test_contributing_says_what_the_base_branch_scanner_cannot_stop():
    """The workflow file of a pull request comes from the pull request: it can skip these steps or read the secret.
    Only review stops that, and the docs must say so instead of promising more."""
    text = " ".join((REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8").split())
    assert "cannot weaken the check that judges it" not in text
    assert ".github/" in text and "by hand" in text and "workflow" in text


def test_pull_requests_must_add_clean_commits_but_the_old_history_does_not_block_them():
    steps = workflow()["jobs"]["privacy"]["steps"]
    new = next(s for s in steps if "origin/${BASE_REF}..HEAD" in s.get("run", ""))
    assert new["if"] == "${{ github.event_name == 'pull_request' }}" and "continue-on-error" not in new
    assert "--ci" in new["run"] and "--history" in new["run"]
    whole = next(s for s in steps if s.get("run", "").strip() == 'python "$SCANNER" --ci --history')
    assert whole["continue-on-error"] == "${{ github.event_name == 'pull_request' }}"     # hard on pushes to main


def test_the_terms_secret_only_goes_to_privacy_steps_and_never_to_the_build():
    jobs = workflow()["jobs"]
    holders = {name for name, job in jobs.items() if "secrets.PERSONAL_TERMS" in yaml.safe_dump(job)}
    assert holders == {"privacy", "privacy-zip"}, holders
    for name in holders:
        for step in jobs[name]["steps"]:
            if "secrets.PERSONAL_TERMS" in yaml.safe_dump(step):
                if step.get("env", {}).get("HAS_TERMS") == "${{ secrets.PERSONAL_TERMS != '' }}":
                    assert "secrets." not in step["run"] and len(step["env"]) == 1, step     # only whether it is set
                    continue
                assert "privacy_scan.py" in step["run"] or "SCANNER" in step["run"], step         # only the scanner's own steps
    build_scan = next(s for s in jobs["build"]["steps"] if "privacy_scan.py" in s.get("run", ""))
    assert "--zip" in build_scan["run"] and "--secrets-only" in build_scan["run"]
    names = [s.get("name", "") + s.get("uses", "") for s in jobs["build"]["steps"]]
    assert [i for i, s in enumerate(jobs["build"]["steps"]) if s is build_scan][0] < next(i for i, n in enumerate(names) if "upload-artifact" in n)


def test_the_zip_is_checked_with_the_terms_in_its_own_job_and_the_release_waits_for_it():
    jobs = workflow()["jobs"]
    job = jobs["privacy-zip"]
    assert job["needs"] == "build" and job["permissions"] == {"contents": "read"}
    assert any(str(s.get("uses", "")).startswith("actions/download-artifact") and s["with"]["name"] == "setup-zip" for s in job["steps"])
    scan = next(s for s in job["steps"] if "--zip" in s.get("run", "") and "SCANNER" in s["run"])
    # a missing or empty terms secret stops the release (--ci: an error on main), it never ships unchecked
    assert "--ci" in scan["run"].split("#")[0] and scan["env"]["KAIRO_PERSONAL_TERMS"] == "${{ secrets.PERSONAL_TERMS }}"
    # ... and a pull request from this repository is red already, before it is merged (forks get no secrets)
    guard = next(s for s in job["steps"] if s.get("env", {}).get("HAS_TERMS"))
    assert job["steps"].index(guard) < job["steps"].index(scan)
    assert "github.event.pull_request.head.repo.full_name == github.repository" in guard["if"] and "push" in guard["if"]
    assert "exit 1" in guard["run"] and "::error::" in guard["run"] and "PERSONAL_TERMS" in guard["run"]
    zip_gl = next(r for r in run_steps(job) if "gitleaks/gitleaks" in r and " dir " in r)
    assert "--verbose" not in zip_gl and "--max-archive-depth" in zip_gl and "unzip" in zip_gl
    release = jobs["release"]
    assert "privacy-zip" in release["needs"] and "build" in release["needs"]
    assert release["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"


def test_existing_ci_jobs_are_still_there():
    jobs = set(workflow()["jobs"])
    assert {"build", "server", "voice", "voice-windows", "voice-install-windows", "memory", "browser", "release-tools",
            "web", "windows-install", "release", "privacy", "privacy-zip"} <= jobs
    needs = workflow()["jobs"]["release"]["needs"]
    for old in ("build", "server", "voice", "voice-windows", "voice-install-windows", "browser", "web", "release-tools", "windows-install"):
        assert old in needs


def test_build_release_scans_the_zip_and_removes_it_if_it_fails():
    sh = (REPO_ROOT / "scripts" / "build-release.sh").read_text(encoding="utf-8")
    assert "privacy_scan.py --zip" in sh
    assert sh.index("privacy_scan.py --zip") < sh.index('echo "$version" > "$out/VERSION"')
    assert 'rm -f "$zip"' in sh


def test_gitignore_keeps_personal_data_out():
    lines = {ln.strip() for ln in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    for must in ("private/", "data/", "devdata/", "*.local.*", ".env", "*.sqlite", "secrets/", "vault/", ".private/", "*.env", ".envrc",
                 ".env-*", "*.db", "*.sqlite*", "capture-token*"):
        assert must in lines, must
    assert "!.env.example" in lines


def test_everything_gitignored_for_privacy_is_also_forbidden_by_the_scanner():
    import importlib.util
    spec = importlib.util.spec_from_file_location("privacy_scan_for_ignore", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["privacy_scan_for_ignore"] = mod
    spec.loader.exec_module(mod)
    for sample in ("private/x", "data/x", "devdata/x", "a.local.yaml", ".env", ".env.production", "secrets/x", "vault/x", ".private/x",
                   "prod.env", ".envrc", ".env-prod", "x.db", "x.sqlite", "x.sqlite.bak", "capture-token.txt"):
        assert mod.forbidden_path(sample), sample


def test_gitleaks_config_extends_the_defaults_and_every_exception_names_its_rule():
    import tomllib
    cfg = tomllib.loads((REPO_ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))
    assert cfg["extend"]["useDefault"] is True
    assert "allowlist" not in cfg                          # no exception that covers every rule
    for entry in cfg.get("allowlists", []):
        assert entry.get("targetRules"), entry.get("description")
    ids = {r["id"] for r in cfg.get("rules", [])}
    assert "kairo-vapid-private-key" in ids                # a rule of our own for the key shape the defaults do not know


def test_contributing_documents_the_hook_setup():
    for doc in ("README.md", "CONTRIBUTING.md"):
        text = (REPO_ROOT / doc).read_text(encoding="utf-8")
        assert "git config core.hooksPath scripts/hooks" in text, doc
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "--install-hooks" in contributing and "old branch" in contributing.lower()
    assert "UTF-8" in contributing and "PERSONAL_TERMS" in contributing


# ---------------------------------------------------------------- the PC's config folder

def test_the_terms_list_in_the_config_folder_is_not_copied_into_the_effective_config(tmp_path):
    """On kairo-home the list lives in data\\config, which is also the owner's config-overrides folder: Kairo copies the
    owner's extra files from there into config-effective at every start. The list is not config."""
    o = fresh_overrides(tmp_path)
    (o / cl.PERSONAL_TERMS).write_text(f"{NAME}\n", encoding="utf-8")
    (o / "my-notes.txt").write_text("an extra file of the owner\n", encoding="utf-8")
    out, _report = build_config(tmp_path, o)
    assert not (out / "personal-terms.txt").exists()
    assert (out / "my-notes.txt").is_file()                                      # other extra files are still carried over
    assert cl.PERSONAL_TERMS in cl.NOT_CONFIG
