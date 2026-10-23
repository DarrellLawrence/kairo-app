"""Static checks on the Windows installer files (they only run on kairo-home).

A double-clicked .bat that cmd.exe can't parse, or a .ps1 that Windows
PowerShell 5.1 reads in the wrong code page, closes before it can say why.
"""

from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[2] / "installer"
BATS = sorted(INSTALLER.rglob("*.bat"))
CMDS = sorted(INSTALLER.rglob("*.cmd"))
SCRIPTS = sorted(list(INSTALLER.rglob("*.ps1")) + list(INSTALLER.rglob("*.psm1")))


def test_found_the_files():
    assert len(BATS) == 6
    assert any(p.name == "Install Kairo.bat" for p in BATS)


@pytest.mark.parametrize("path", BATS + CMDS + SCRIPTS, ids=lambda p: p.name)
def test_ascii_crlf_no_bom(path):
    data = path.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), "no BOM"
    data.decode("ascii")  # PowerShell 5.1 reads BOM-less files as ANSI; keep them plain ASCII
    assert b"\n" not in data.replace(b"\r\n", b""), "CRLF line endings only"


@pytest.mark.parametrize("path", BATS, ids=lambda p: p.name)
def test_bat_is_robust(path):
    text = path.read_text("ascii")
    # Elevates itself (one UAC prompt), finds its files from its own folder.
    assert 'fltmc.exe" >nul 2>&1' in text and "-Verb RunAs" in text
    assert 'set "KAIRO_HERE=%~dp0"' in text and "%~dp0" not in text.replace('set "KAIRO_HERE=%~dp0"', "")
    # Full path to Windows PowerShell, bypass for this run only, unblocks its files.
    assert r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" in text
    assert "Set-ExecutionPolicy" not in text
    assert text.count("-ExecutionPolicy Bypass") == 3
    assert "Unblock-File" in text
    # Never closes on an error.
    assert ":failed" in text and "pause >nul" in text and "exit /b %KAIRO_RC%" in text
    # Paths only ever used inside quotes (spaces, parentheses, ampersands).
    for line in text.splitlines():
        if line.lower().startswith(("rem", "echo", "set ")):
            continue
        for var in ("%KAIRO_HERE%", "%KAIRO_PS%", "%KAIRO_ROOT%", "%KAIRO_LOG%"):
            for i in range(len(line)):
                if line.startswith(var, i):
                    assert line[:i].count('"') % 2 == 1, f"unquoted {var}: {line}"


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_paths_variable_never_shadowed(path):
    # PowerShell names are case-insensitive: `$p = ...` silently replaces the
    # Kairo paths object `$P` (this broke vault sync once).
    import re
    for n, line in enumerate(path.read_text("ascii").splitlines(), 1):
        if re.search(r"\$p\s*=[^=]", line, re.IGNORECASE) and "Get-KairoPaths" not in line:
            pytest.fail(f"{path.name}:{n} assigns $P/$p: {line.strip()}")


# ---------------------------------------------------------------- kairo.cmd: `kairo diag` from any prompt

def read(name: str) -> str:
    return (INSTALLER / name).read_text("ascii")


def test_kairo_cmd_runs_the_diag_module_with_kairos_own_python_and_only_reads():
    assert [p.name for p in CMDS] == ["kairo.cmd"]
    text = (INSTALLER / "bat" / "kairo.cmd").read_text("ascii")
    # the release's venv, from the folder the server runs in (the `app` package is found from there)
    assert r'set "KAIRO_PY=%KAIRO_ROOT%\app\current\venv\Scripts\python.exe"' in text
    assert r'pushd "%KAIRO_ROOT%\app\current\server"' in text and "popd" in text
    assert r'"%KAIRO_PY%" -m app.diag --env-file "%KAIRO_ROOT%\data\kairo.env" %KAIRO_ARGS%' in text
    assert 'set "KAIRO_ROOT=%%~fI"' in text and "setlocal EnableExtensions DisableDelayedExpansion" in text
    assert "PYTHONDONTWRITEBYTECODE=1" in text                      # run by an administrator: no .pyc files in the release
    assert "exit /b %KAIRO_RC%" in text and "if /i not \"%~1\"==\"diag\" goto usage" in text
    # no administrator rights, nothing installed or changed
    for forbidden in ("fltmc", "RunAs", "Set-ExecutionPolicy", "reg add", "icacls", "netsh", "schtasks", "pip "):
        assert forbidden.lower() not in text.lower(), forbidden
    for line in text.splitlines():                                   # paths only ever used inside quotes
        if line.lower().startswith(("rem", "echo", "set ")):
            continue
        for var in ("%KAIRO_PY%", "%KAIRO_ROOT%"):
            for i in range(len(line)):
                if line.startswith(var, i):
                    assert line[:i].count('"') % 2 == 1, f"unquoted {var}: {line}"


def test_kairo_cmds_usage_names_exactly_the_flags_diag_has():
    import re

    from app import diag
    usage = re.findall(r"^echo\s+(--[a-z-]+)", (INSTALLER / "bat" / "kairo.cmd").read_text("ascii"), re.M)
    flags = {a for a in diag.parser()._option_string_actions if a.startswith("--")} - {"--help", "--env-file"}
    assert set(usage) == flags


def test_the_installer_places_kairo_cmd_in_bin_on_the_machine_path_and_the_uninstaller_takes_it_away():
    psm1, install, uninstall = read("Kairo.psm1"), read("Install-Kairo.ps1"), read("Uninstall-Kairo.ps1")
    assert "Bin          = Join-Path $Root 'bin'" in psm1
    assert "Install-Launcher $P (Join-Path $PSScriptRoot 'bat\\kairo.cmd')" in install
    # copied before the copies in installer\bat are removed, and a failure never fails an install
    assert install.index("Install-Launcher") < install.index("Remove-Item -Recurse -Force $batCopies")
    body = psm1[psm1.index("function Install-Launcher"):psm1.index("function Remove-Launcher")]
    assert "'Machine')" in body and "'User')" not in body and "catch { Write-Warn" in body
    assert "Remove-Launcher $P" in uninstall
    assert "Remove-Item -Recurse -Force $P.Bin" in psm1


def test_the_release_zip_carries_installer_bat_and_so_kairo_cmd():
    script = (INSTALLER.parent / "scripts" / "build-release.sh").read_text(encoding="utf-8")
    assert 'cp -r installer/bat "$root/installer/bat"' in script


def test_the_windows_ci_checks_kairo_cmd():
    ci = (INSTALLER / "ci" / "Test-Install.ps1").read_text("ascii")
    for needle in ("'bin\\kairo.cmd'", "on the machine PATH, once", "cannot change it", "diag --json --local", "off the machine PATH again"):
        assert needle in ci, needle
