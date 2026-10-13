import os
import socket
import subprocess
import sys
import time
import urllib.request

from app.serve import HOST, load_env_file
from tests.conftest import FAKE_CLAUDE, SERVER_DIR, alembic_config

from alembic import command


def test_env_file_parsing(tmp_path, monkeypatch):
    f = tmp_path / "kairo.env"
    f.write_text('\ufeff# comment\nKAIRO_A=1\n\nKAIRO_B = "C:\\\\Kairo\\\\data"\nKAIRO_C=\'x y\'\nnot a line\n', encoding="utf-8")
    monkeypatch.delenv("KAIRO_A", raising=False)
    loaded = load_env_file(f)
    assert loaded == {"KAIRO_A": "1", "KAIRO_B": "C:\\\\Kairo\\\\data", "KAIRO_C": "x y"}
    assert os.environ["KAIRO_A"] == "1"


def test_binds_loopback_only():
    assert HOST == "127.0.0.1"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_serve_starts_and_answers_health(tmp_path):
    data = tmp_path / "data"
    env_file = tmp_path / "kairo.env"
    db_url = f"sqlite:///{(data / 'db' / 'kairo.sqlite').as_posix()}"
    env_file.write_text(f"KAIRO_DATA_DIR={data}\nKAIRO_DB_URL={db_url}\nKAIRO_CLAUDE_BIN={FAKE_CLAUDE}\n", encoding="utf-8")
    command.upgrade(alembic_config(db_url), "head")
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "app.serve", "--env-file", str(env_file), "--port", str(port)],
                            cwd=SERVER_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        body = None
        for _ in range(60):
            try:
                body = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2).read().decode()
                break
            except OSError:
                time.sleep(0.25)
        assert body is not None and '"status":"ok"' in body
        assert (data / "logs" / "kairo.log").is_file()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
