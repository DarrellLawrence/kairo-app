"""scripts/deploy-remote.sh against a throwaway repo, with docker and the health check stubbed."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app.settings import REPO_ROOT

REMOTE = REPO_ROOT / "scripts" / "deploy-remote.sh"
DEPLOY = REPO_ROOT / "scripts" / "deploy.sh"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(work: Path, migrations: list[str], msg: str) -> str:
    versions = work / "server" / "migrations" / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    for name in migrations:
        (versions / name).write_text("# migration\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", msg)
    git(work, "push", "-q", "origin", "HEAD:main")
    return git(work, "rev-parse", "HEAD")


@pytest.fixture
def env(tmp_path: Path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "t")
    first = commit(work, ["0001_initial.py"], "one")

    deploy_dir = tmp_path / "opt-kairo"
    git(tmp_path, "clone", "-q", str(origin), str(deploy_dir))

    data = tmp_path / "srv"
    (data / "db").mkdir(parents=True)
    con = sqlite3.connect(data / "db" / "kairo.sqlite")
    con.execute("create table t(x)")
    con.commit()
    con.close()
    (deploy_dir / ".env").write_text(f"KAIRO_HOST_DATA_DIR={data}\n")
    (deploy_dir / ".git" / "info" / "exclude").write_text(".env\n")

    calls = tmp_path / "compose.log"
    compose = tmp_path / "compose"
    compose.write_text(f'#!/bin/sh\necho "$@" >> {calls}\n')
    compose.chmod(0o755)
    health_flag = tmp_path / "healthy"
    health_flag.write_text("")

    run_env = dict(
        os.environ,
        KAIRO_DEPLOY_DIR=str(deploy_dir),
        KAIRO_DEPLOY_COMPOSE=str(compose),
        KAIRO_DEPLOY_HEALTH_CMD=f"test -f {health_flag}",
        KAIRO_DEPLOY_HEALTH_WAIT_S="3",
    )
    return dict(work=work, deploy_dir=deploy_dir, data=data, calls=calls, health=health_flag, env=run_env, first=first)


def remote(e, *args):
    return subprocess.run(["bash", str(REMOTE), *args], env=e["env"], capture_output=True, text=True)


def test_deploy_checks_out_backs_up_and_tags(env):
    sha = commit(env["work"], ["0002_more.py"], "two")
    r = remote(env, "deploy", sha)
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(env["deploy_dir"], "rev-parse", "HEAD") == sha
    assert git(env["deploy_dir"], "tag", "--list", "deploy-*")
    assert "up -d --build" in env["calls"].read_text()
    assert list((env["data"] / "db" / "backups").glob("pre-deploy-*.sqlite"))


def test_failed_health_check_rolls_back(env):
    before = git(env["deploy_dir"], "rev-parse", "HEAD")
    sha = commit(env["work"], ["0002_more.py"], "two")
    env["health"].unlink()
    r = remote(env, "deploy", sha)
    assert r.returncode == 1
    assert git(env["deploy_dir"], "rev-parse", "HEAD") == before
    assert "alembic downgrade 0001" in env["calls"].read_text()
    assert git(env["deploy_dir"], "tag", "--list", "deploy-*") == ""


def test_rollback_goes_to_previous_deploy_and_downgrades(env):
    assert remote(env, "deploy", env["first"]).returncode == 0
    import time

    time.sleep(1.1)  # deploy tags are per second
    second = commit(env["work"], ["0002_more.py"], "two")
    assert remote(env, "deploy", second).returncode == 0
    r = remote(env, "rollback")
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(env["deploy_dir"], "rev-parse", "HEAD") == env["first"]
    assert "alembic downgrade 0001" in env["calls"].read_text()
    assert len(git(env["deploy_dir"], "tag", "--list", "deploy-*").splitlines()) == 1


def test_rollback_without_previous_deploy_refuses(env):
    r = remote(env, "rollback")
    assert r.returncode == 1
    assert "no previous deploy" in r.stdout


def test_deploy_sh_refuses_dirty_tree(tmp_path):
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{REPO_ROOT}", str(clone))
    (clone / "scripts").mkdir(exist_ok=True)
    for f in (DEPLOY, REMOTE):
        (clone / "scripts" / f.name).write_text(f.read_text())
    (clone / "dirty.txt").write_text("x")
    r = subprocess.run(["bash", str(clone / "scripts" / "deploy.sh")], cwd=clone, capture_output=True, text=True,
                       env=dict(os.environ, KAIRO_DEPLOY_HOST="local"))
    assert r.returncode == 1
    assert "Uncommitted changes" in r.stderr
