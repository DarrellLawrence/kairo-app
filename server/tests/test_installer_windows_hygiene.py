"""G8 in the Windows installer (voice recipe 9). These only run on kairo-home, so here they are checked as text (pwsh is not installed on
the build machine, nothing below was executed by PowerShell): HF_HUB_OFFLINE goes into kairo.env and nowhere else."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "installer"
APP = REPO / "server" / "app"


def read(rel: str) -> str:
    return (INSTALLER / rel).read_text("ascii")


def function_body(text: str, name: str) -> str:
    """From `function <name>` to its closing brace at the start of a line."""
    start = text.index(f"function {name}")
    return text[start:text.index("\n}", start) + 2]


# ---------------------------------------------------------------- HF_HUB_OFFLINE: kairo.env only

def test_the_installer_writes_hf_hub_offline_into_kairo_env():
    install = read("Install-Kairo.ps1")
    block = install[install.index("$envValues = @{"):install.index("}", install.index("$envValues = @{"))]
    assert re.search(r"HF_HUB_OFFLINE\s*=\s*'1'", block)


def test_hf_hub_offline_is_named_in_the_installer_only_where_it_is_written_and_where_ci_checks_it():
    """The installer itself downloads models through huggingface_hub (Whisper): it must never run with the variable set."""
    named = sorted(str(p.relative_to(INSTALLER)).replace(os.sep, "/") for p in INSTALLER.rglob("*")
                   if p.is_file() and p.suffix in (".ps1", ".psm1", ".bat", ".cmd") and "HF_HUB_OFFLINE" in p.read_text("ascii"))
    assert named == ["Install-Kairo.ps1", "ci/Test-Install.ps1"]


def test_the_installers_own_python_environment_takes_only_the_four_model_names_from_kairo_env():
    psm1 = read("Kairo.psm1")
    assert "$script:EnvFromFile = @('KAIRO_WHISPER_MODEL', 'KAIRO_WHISPER_CPU_MODEL', 'KAIRO_KOKORO_VOICE', 'KAIRO_PIPER_VOICE')" in psm1


def test_ci_checks_kairo_env_and_the_installers_environment():
    ci = read("ci/Test-Install.ps1")
    for needle in ("HF_HUB_OFFLINE=1", "'HF_HUB_OFFLINE=1'", "-not $env:HF_HUB_OFFLINE"):
        assert needle in ci, needle


def test_the_server_gets_the_variable_from_kairo_env_when_it_starts(tmp_path, monkeypatch):
    from app.serve import load_env_file
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    env = tmp_path / "kairo.env"
    env.write_text("# Written by the Kairo installer.\nHF_HUB_OFFLINE=1\nKAIRO_PORT=8000\n", encoding="utf-8")
    assert load_env_file(env)["HF_HUB_OFFLINE"] == "1" and os.environ["HF_HUB_OFFLINE"] == "1"


# Every place in the server that can reach the Hugging Face hub library at run time, as of this audit. HF_HUB_OFFLINE=1 makes each of
# them fail fast (no connection) instead of downloading inside a request. A new file here must be audited first: add it with the reason.
HUB_LIBRARY = {
    "voice/__init__.py": "faster-whisper: WhisperModel(<local folder or model name>) and download_model() (the installer's download)",
}
HUB_WORDS = ("huggingface_hub", "from_pretrained", "hf_hub_download", "snapshot_download", "WhisperModel(", "download_model(")


def test_the_only_run_time_use_of_the_hub_library_is_faster_whispers_model_load():
    found = {}
    for f in sorted(APP.rglob("*.py")):
        text = f.read_text(encoding="utf-8")
        words = [w for w in HUB_WORDS if w in text]
        if words:
            found[f.relative_to(APP).as_posix()] = words
    assert set(found) == set(HUB_LIBRARY), found
    assert set(found["voice/__init__.py"]) == {"WhisperModel(", "download_model("}


def test_every_other_download_is_plain_https_to_a_fixed_url_that_the_variable_does_not_touch():
    """voicellm.py, semantic.py and the speaker model use urllib with fixed URLs (outbound.MODULES), not the hub library."""
    for rel in ("voicellm.py", "semantic.py"):
        text = (APP / rel).read_text(encoding="utf-8")
        assert "huggingface_hub" not in text and "urllib.request" in text, rel


def test_with_the_variable_set_a_missing_whisper_model_fails_at_once_without_a_connection(tmp_path):
    """The first request used to download 1.5 GB when the model folder was missing; now it is an error in a fraction of a second.
    huggingface_hub reads the variable once at import, so this runs in a process of its own with a socket audit hook."""
    pytest.importorskip("faster_whisper")
    code = textwrap.dedent('''
        import json, sys, tempfile, time
        connections = []
        sys.addaudithook(lambda ev, args: connections.append(str(args[1])) if ev == "socket.connect" else None)
        from faster_whisper import WhisperModel
        empty = tempfile.mkdtemp()
        began, error = time.perf_counter(), None
        try:
            WhisperModel("tiny", device="cpu", compute_type="int8", download_root=empty)
        except Exception as exc:
            error = type(exc).__name__
        print(json.dumps({"error": error, "seconds": time.perf_counter() - began, "connections": connections}))
    ''')
    env = {**os.environ, "HF_HUB_OFFLINE": "1"}
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120, env=env)
    import json
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["error"] is not None and got["connections"] == [] and got["seconds"] < 15, got
