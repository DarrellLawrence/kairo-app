"""Static checks on the Windows installer files (they only run on kairo-home).

A double-clicked .bat that cmd.exe can't parse, or a .ps1 that Windows
PowerShell 5.1 reads in the wrong code page, closes before it can say why.
"""

from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[2] / "installer"
BATS = sorted(INSTALLER.rglob("*.bat"))
SCRIPTS = sorted(list(INSTALLER.rglob("*.ps1")) + list(INSTALLER.rglob("*.psm1")))


def test_found_the_files():
    assert len(BATS) == 6
    assert any(p.name == "Install Kairo.bat" for p in BATS)


@pytest.mark.parametrize("path", BATS + SCRIPTS, ids=lambda p: p.name)
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
