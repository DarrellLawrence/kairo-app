"""G8 in the Windows installer (voice recipe 9). These only run on kairo-home, so here they are checked as text (pwsh is not installed on
the build machine, nothing below was executed by PowerShell): HF_HUB_OFFLINE goes into kairo.env and nowhere else; NVIDIA's CUDA sysmem
fallback policy is written only when the owner passes -CudaSysmemPolicy to Install-Kairo, never by an update and never in CI."""

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


def test_ci_checks_kairo_env_the_installers_environment_and_the_driver_script():
    ci = read("ci/Test-Install.ps1")
    for needle in ("HF_HUB_OFFLINE=1", "'HF_HUB_OFFLINE=1'", "-not $env:HF_HUB_OFFLINE",
                   "system.sysmem", "nvsettings.py", "Set-CudaSysmemPolicy"):
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
    # faster-whisper 1.2.1: WhisperModel(<model folder, else the model name>) and download_model() (the installer's download). Inside it
    # the hub is reached only for a name (snapshot_download) and, if tokenizer.json is missing from the folder, for the "openai/whisper-tiny"
    # tokenizer; the download always fetches tokenizer.json (its allow_patterns), so a complete folder never reaches the hub.
    "voice/__init__.py": "faster-whisper model load and download",
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


# ---------------------------------------------------------------- the sysmem policy: the owner's opt-in

def test_the_switch_exists_is_off_by_default_and_is_passed_on_when_the_installer_restarts_itself_as_administrator():
    install = read("Install-Kairo.ps1")
    assert "[switch]$CudaSysmemPolicy," in install or "[switch]$CudaSysmemPolicy\r\n" in install
    assert not re.search(r"\$CudaSysmemPolicy\s*=\s*\$true", install, re.I)
    assert "if ($CudaSysmemPolicy) { $passArgs += '-CudaSysmemPolicy' }" in install
    assert "-CudaSysmemPolicy" in install.split("param(")[0]            # the header comment says how to use it


def test_it_runs_only_for_an_interactive_install_on_a_real_pc():
    install = read("Install-Kairo.ps1")
    calls = [ln.strip() for ln in install.splitlines() if "Set-CudaSysmemPolicy" in ln]
    assert len(calls) == 1
    # inside the `if (-not $CI)` block of step 4, and only for an interactive run (not -Update, which is what the auto-updater passes)
    step4 = install[install.index("# 4. Windows settings"):install.index("# 5. Kairo itself")]
    assert "if (-not $CI) {" in step4 and calls[0] in step4
    assert "$CudaSysmemPolicy -and $interactive" in calls[0]
    assert "$sourceApp" in calls[0] and "server\\app\\nvsettings.py" in calls[0] and "$python" in calls[0]


@pytest.mark.parametrize("name", ["Update-Kairo.ps1", "Kairo-AutoUpdate.ps1", "Kairo-BootCheck.ps1", "Kairo.psm1", "KairoSync.psm1"])
def test_no_update_and_no_boot_check_ever_writes_the_driver_setting(name):
    text = read(name)
    assert "CudaSysmemPolicy" not in text and "nvsettings" not in text


def test_the_function_writes_then_reads_back_in_a_fresh_process_and_compares_and_never_fails_the_install():
    body = function_body(read("KairoSetup.psm1"), "Set-CudaSysmemPolicy")
    assert "@('set', '1')" in body and "@('get')" in body and body.index("'set', '1'") < body.index("'get'")
    assert "'^policy=1(\\s|$)'" in body                                  # the read-back must say 1
    assert "Write-Ok" in body and "Write-Warn" in body and "return $true" in body and "return $false" in body
    assert "try {" in body and "} catch {" in body
    outside = body.replace(body[body.index("try {"):body.index("} catch {")], "")
    assert "throw" not in outside                                        # only inside the try: a problem is a warning
    helper = function_body(read("KairoSetup.psm1"), "Get-SysmemPolicyLine")
    assert "2>&1" not in body and "2>&1" not in helper                    # Windows PowerShell 5.1 + ErrorAction Stop would throw
    assert "& $Python -I $ScriptPath @Arguments" in helper                    # isolated Python, the script by path (stdlib only)


def test_a_driver_setting_is_never_part_of_the_ci_install():
    install = read("Install-Kairo.ps1")
    step4 = install[install.index("# 4. Windows settings"):install.index("# 5. Kairo itself")]
    assert step4.index("if (-not $CI) {") < step4.index("Set-CudaSysmemPolicy")


def test_the_driver_script_travels_in_the_release_zip_and_in_the_release_folder():
    script = (REPO / "scripts" / "build-release.sh").read_text(encoding="utf-8")
    assert "cp -r server/app " in script and (APP / "nvsettings.py").is_file()
