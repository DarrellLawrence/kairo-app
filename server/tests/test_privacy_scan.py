"""scripts/privacy_scan.py: personal data and secrets can never get into the repo or a release again
(owner's master prompt 0.6: the code may become public, all personal data lives on the PC only).

Every name, place and key here is made up. They are built from pieces (j) so that this file does not
contain any of them, because one test runs the scanner over the whole repo with these terms and it must
come out clean."""

from __future__ import annotations

import base64
import bz2
import gzip
import importlib.util
import io
import json
import lzma
import os
import quopri
import shutil
import struct
import subprocess
import sys
import tarfile
import urllib.parse
import zipfile
import zlib
from pathlib import Path

import pytest

from app.settings import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "privacy_scan.py"
HOOKS = REPO_ROOT / "scripts" / "hooks"


def j(*parts: str) -> str:
    return "".join(parts)


NAME = j("Jor", "dan Test", "person")                 # a made-up full name
PLACE = j("Example", "town")                          # a made-up place
UML = j("Mül", "lerhausen")                           # made-up town with an umlaut
STRASSE = j("Maß", "tal")                             # made-up valley with an sz
UML_ASCII = j("Muel", "lerhausen")                    # the same town the way a keyboard without umlauts writes it
STRASSE_ASCII = j("Mas", "stal")
ZORB = j("Zor", "blax")                               # leak marker for the regex tests

# Fake credentials. Shapes are real, values are made up.
GH_PAT = j("gh", "p_", "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6")
GH_FINE = j("github", "_pat_", "11AB3dE6gH9jK2mN5pQ8sT1v_W4yZ7bC0eF3hJ6kL9nP2rS5tU8wX1zA4cD7fG0")
GOCSPX = j("GOC", "SPX-", "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7b")
TSKEY = j("ts", "key-auth-", "kAbCdE1CNTRL-", "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7b")
OPENAI = j("s", "k-proj-", "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6")
AWS = j("AK", "IA", "Q7W3E9R2T5Y8U1I4")
GOOGLE_KEY = j("AI", "za", "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q")
PEM_RSA = j("-----BEGIN ", "RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Qu\n-----END ", "RSA PRIVATE KEY-----")
PEM_VAPID = j("-----BEGIN ", "PRIVATE KEY-----\nMIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg\n-----END ", "PRIVATE KEY-----")


@pytest.fixture(scope="module")
def ps():
    assert SCRIPT.is_file(), "scripts/privacy_scan.py is missing"
    spec = importlib.util.spec_from_file_location("privacy_scan", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["privacy_scan"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- helpers

def write(path: Path, content: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)
    return path


def clean_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """The scanner's world with nothing of the developer's: no real terms list, no CI variables."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("KAIRO_PERSONAL", "GITHUB_", "GIT_")) and k not in {"CI", "XDG_CONFIG_HOME"}}
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.touch()
    env.update(HOME=str(home), USERPROFILE=str(home), KAIRO_DATA_DIR=str(tmp_path / "no-data"),
               GIT_CONFIG_GLOBAL=str(gitconfig), GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="Test Author", GIT_AUTHOR_EMAIL="author@example.com",
               GIT_COMMITTER_NAME="Test Committer", GIT_COMMITTER_EMAIL="committer@example.com",
               PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    env.update(extra)
    return env


def scan(tmp_path: Path, *args: str, terms: list[str] | None = None, cwd: Path | None = None,
         **env_extra: str) -> tuple[int, str]:
    env = clean_env(tmp_path, **env_extra)
    if terms is not None:
        env["KAIRO_PERSONAL_TERMS"] = "\n".join(terms)
    p = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd or tmp_path, env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout + p.stderr


def git(repo: Path, tmp_path: Path, *args: str, **env_extra: str) -> str:
    p = subprocess.run(["git", *args], cwd=repo, env=clean_env(tmp_path, **env_extra), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    assert p.returncode == 0, f"git {args}: {p.stderr}"
    return p.stdout


def make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    git(repo, tmp_path, "init", "-q", "-b", "main")
    git(repo, tmp_path, "config", "core.autocrlf", "false")
    git(repo, tmp_path, "config", "commit.gpgsign", "false")
    return repo


def commit(repo: Path, tmp_path: Path, message: str, files: dict[str, str | bytes] | None = None,
           remove: list[str] | None = None, **env_extra: str) -> None:
    for rel, content in (files or {}).items():
        write(repo / rel, content)
    for rel in remove or []:
        (repo / rel).unlink()
    git(repo, tmp_path, "add", "-A", "-f")
    git(repo, tmp_path, "commit", "-q", "--allow-empty", "-m", message, **env_extra)


def stage(repo: Path, tmp_path: Path, *paths: str) -> None:
    git(repo, tmp_path, "add", "-f", *paths)


def png_with_text(key: bytes, text: bytes, compressed: bool = False) -> bytes:
    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    meta = chunk(b"zTXt", key + b"\x00\x00" + zlib.compress(text)) if compressed else chunk(b"tEXt", key + b"\x00" + text)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + meta + chunk(b"IDAT", zlib.compress(b"\x00\x00")) + chunk(b"IEND", b"")


def wav_with_info(text: bytes) -> bytes:
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 16000, 2, 16)
    data = b"\x00\x00" * 8
    inam = b"INAM" + struct.pack("<I", len(text) + 1) + text + b"\x00"
    if len(inam) % 2:
        inam += b"\x00"
    lst = b"LIST" + struct.pack("<I", 4 + len(inam)) + b"INFO" + inam
    body = b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(data)) + data + lst
    return b"RIFF" + struct.pack("<I", len(body)) + body


def pdf_with(author: str | None, content: bytes | None = None) -> bytes:
    """A small PDF: the author in the (plain) Info dictionary, text in a Flate content stream (b"BT [(Jor)-20(dan)] TJ ET")."""
    stream = b""
    if content:
        packed = zlib.compress(content)
        stream = b"4 0 obj << /Length %d /Filter /FlateDecode >>\nstream\n" % len(packed) + packed + b"\nendstream endobj\n"
    info = b"3 0 obj << /Author (" + author.encode() + b") >> endobj\n" if author else b""
    return (b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
            b"2 0 obj << /Type /Pages /Kids [] /Count 0 >> endobj\n" + info + stream + b"trailer << /Root 1 0 R /Info 3 0 R >>\n%%EOF\n")


def no_leak(out: str, *secrets: str) -> None:
    low = out.lower()
    for s in secrets:
        assert s.lower() not in low, "the scanner printed something it must never print"


# ---------------------------------------------------------------- matching rules (unit)

def test_fold_drops_every_invisible_character_but_keeps_the_spaces(ps):
    """Format characters (Cf), combining and enclosing marks (Mn, Me) and controls other than whitespace inside a word
    must not hide it; a tab, a line break or a form feed still separates two words."""
    import unicodedata
    plain = ps.fold(PLACE)
    missed = [hex(cp) for cp in range(0x110000)
              if (unicodedata.category(chr(cp)) in ("Cf", "Mn", "Me")
                  or (unicodedata.category(chr(cp)) == "Cc" and not chr(cp).isspace()))
              and ps.fold(PLACE[:4] + chr(cp) + PLACE[4:]) != plain]
    assert missed == [], missed[:20]
    for sep in "\t\n\r\x0b\x0c\x1e\x1f\x85 ":
        assert ps.fold(f"ab{sep}cd") == f"ab{sep}cd", repr(sep)
    assert ps.TermList.parse(PLACE).find(PLACE[:4] + "\t" + PLACE[4:]) == []


def test_fold_umlauts_case_and_accents(ps):
    assert ps.fold("Müller") == "mueller"
    assert ps.fold("ÖL Äpfel Üben") == "oel aepfel ueben"
    assert ps.fold("Straße") == ps.fold("STRASSE") == "strasse"
    assert ps.fold("Mu\u0308ller") == "mueller"          # u + combining diaeresis (macOS file names)
    assert ps.fold("José") == "jose"


@pytest.mark.parametrize("term,text", [
    (UML, UML), (UML, UML.lower()), (UML, UML.upper()), (UML, UML_ASCII), (UML, UML_ASCII.upper()),
    (UML_ASCII, UML),
    (STRASSE, STRASSE_ASCII), (STRASSE, STRASSE_ASCII.upper()), (STRASSE_ASCII, STRASSE), (STRASSE, j("Ma", chr(0xDF), "tal")),
    (j("Bä", "renau"), "Baerenau"), (j("Jö", "rgsen"), "JOERGSEN"),
])
def test_umlaut_folded_matching(ps, term, text):
    t = ps.TermList.parse(term)
    assert t.find(f"met {text}, then left\n") == [(1, 1)]


def test_case_insensitive_and_word_boundaries(ps):
    t = ps.TermList.parse(NAME)
    assert t.find(NAME.upper()) and t.find(NAME.lower())
    assert t.find("(" + NAME + ")") and t.find(NAME + ".") and t.find("x," + NAME)
    assert t.find(NAME.replace(" ", "  ")) == [(1, 1)]           # more than one space
    assert t.find(NAME.replace(" ", "\t")) == [(1, 1)]
    assert t.find(NAME.replace(" ", "\n")) == [(1, 1)]           # a name wrapped over two lines
    # inside a longer word: no hit
    first, last = NAME.split()
    assert not t.find(f"{first}ian {last}")
    assert not t.find(f"{first} {last}s")
    assert not t.find(f"x{first} {last}")
    single = ps.TermList.parse(first)
    assert not single.find(first + "ian")
    assert not single.find("un" + first.lower())
    assert single.find(f"{first}'s")
    assert single.find(f"{first}_x") and single.find(f"{first}2024")       # a user name is still the name
    assert single.find(f"mail {first.lower()}@example.org")


def test_terms_that_start_or_end_with_a_digit_or_a_sign_keep_their_edges(ps):
    phone = ps.TermList.parse("+41 79 000 00 00")
    assert phone.find("call +41 79 000 00 00 now") and phone.find("+41  79 000 00 00")
    num = ps.TermList.parse("1234567")
    assert num.find("id 1234567.") and not num.find("id 91234567") and not num.find("id 12345678")


def test_list_format_comments_blank_lines_and_index(ps):
    t = ps.TermList.parse(f"# my terms\n\n{PLACE}\n   \n# more\n{NAME}  \n")
    assert [r.index for r in t.rules] == [1, 2]
    assert [r.list_line for r in t.rules] == [3, 6]
    assert t.find(f"a\nlives in {PLACE}\nand {NAME}") == [(2, 1), (3, 2)]


def test_list_tolerates_bom_and_crlf(ps):
    t = ps.TermList.parse("\ufeff" + PLACE + "\r\n" + NAME + "\r\n")
    assert len(t.rules) == 2 and t.find(NAME) == [(1, 2)]


def test_regex_rules(ps):
    t = ps.TermList.parse(f"regex:{PLACE}\\s+\\d{{4}}\nregex:\\bM(?:ue|ü|u)ller{ZORB}\\b\n")
    assert t.find(f"{PLACE} 8000") == [(1, 1)]
    assert not t.find(f"{PLACE} abcd")
    assert t.find(f"x Müller{ZORB} y") == [(1, 2)]            # as written
    assert t.find(f"x MUELLER{ZORB} y") == [(1, 2)]           # and with the umlaut folded (a pattern in the folded alphabet)
    assert t.find(f"x Muller{ZORB} y") == [(1, 2)]


def test_a_bad_regex_is_an_error_that_names_the_rule_only(ps):
    with pytest.raises(ps.TermError) as e:
        ps.TermList.parse(f"{NAME}\nregex:{ZORB}(")
    assert e.value.index == 2
    no_leak(str(e.value), ZORB)
    with pytest.raises(ps.TermError):
        ps.TermList.parse("regex:a*")                          # would match everywhere


def test_one_line_can_hit_several_rules(ps):
    t = ps.TermList.parse(f"{PLACE}\n{NAME}")
    assert t.find(f"{NAME} of {PLACE}") == [(1, 1), (1, 2)]


# ---------------------------------------------------------------- forbidden paths and secrets (unit)

@pytest.mark.parametrize("path", [
    "private/profile.md", "a/b/private/x.txt", "data/db/kairo.sqlite", "server/data/x", "devdata/x",
    "x/devdata/y", "config.local.yaml", "a/b/settings.local.json", ".env", "sub/.env", ".env.production",
    "kairo.sqlite", "x/kairo.sqlite3", "db/kairo.sqlite-wal", "personal-terms.txt", "x/personal-terms.local.txt",
    "keys/client_secret_123.apps.googleusercontent.com.json", "a/id_rsa", "cert.p12", "Kairo-Setup/app/private/",
    "win\\data\\x.txt",
])
def test_forbidden_paths(ps, path):
    assert ps.forbidden_path(path), path


@pytest.mark.parametrize("path", [
    ".env.example", "sub/.env.example", "server/app/data_utils.py", "docs/private-notes.md", "web/public/icon.png",
    "config/profile.yaml", "server/tests/test_data.py", "metadata/x.txt", "x/localdata/y", "scripts/hooks/pre-commit",
    "server/app/secretbox.py", "server/app/vault.py", "a/b.local", "environment.md",
])
def test_normal_paths_are_fine(ps, path):
    assert ps.forbidden_path(path) == [], path


@pytest.mark.parametrize("name,text", [
    ("GitHub token", f"x = '{GH_PAT}'"),
    ("GitHub token", f"gho = {j('gh', 'o_', 'aB3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6')}"),
    ("GitHub token", f"GH_TOKEN: {GH_FINE}"),
    ("Google OAuth client secret", f'"client_secret": "{GOCSPX}"'),
    ("Tailscale key", f"TS={TSKEY}"),
    ("API key (sk-)", f"OPENAI={OPENAI}"),
    ("AWS access key", f"aws {AWS} here"),
    ("Google API key", f"key={GOOGLE_KEY}"),
    ("private key", PEM_RSA),
    ("private key", PEM_VAPID),
    ("password/secret/token assignment", j("db_pass", "word = 'q7Xk2Lm9Vb4Zr8Tn3Wp5Hs6'")),
    ("password/secret/token assignment", j("API_TO", "KEN: Zq81vXk3Lm20Pa94Rt77Wc55Ys")),
    ("password/secret/token assignment", j('{"client_', 'secret": "Zq81vXk3L', 'm20Pa94Rt77Wc55Ys"}')),
    ("password/secret/token assignment", j("VAPID_PRIVATE_", "KEY=Zq81vXk3Lm20Pa94Rt77Wc55YsAbCdEf12GhIj34K")),
    ("bearer token", "Authorization: " + j("Bea", "rer ") + "Zq81vXk3Lm20Pa94Rt77Wc55YsAbCd"),
])
def test_secret_detector_finds_real_looking_secrets(ps, name, text):
    found = ps.scan_secrets("first line\n" + text + "\nlast line\n")
    assert found and found[0][1] == name and found[0][0] == 2, found


@pytest.mark.parametrize("text", [
    # placeholders
    'password = "changeme"', 'token = "<your-token-here>"', "secret: ${KAIRO_SECRET}", "api_key = os.environ['KEY']",
    'password: "your-password-goes-here-please-change"', "token = %KAIRO_TOKEN%", "TOKEN=xxxxxxxxxxxxxxxxxxxxxxxx",
    "secret = 'example-secret-value-1234567890'", "password = '********************'", "token = None",
    "password = ''", "GH_TOKEN: ${{ github.token }}", "token = secrets.token_urlsafe(32)", "tokens = 1234567890123456789",
    "secret_path = C:/Kairo/data/secrets/google.json", "token_sha256 = 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "password_field = request.form['password_confirmation_value']",
    # known fake values from the tests
    j("KEY = '", "s", "k-test", "x" * 30, "'"), j("aws ", "AKIA", "IOSFODNN7", "EXAMPLE"),
    j("gh", "p_", "x" * 36), j("GOC", "SPX-", "0" * 28), j("ts", "key-auth-", "x" * 24),
    # words that only look like prefixes
    "task-force-alpha-long-hyphenated-identifier-name", "disk-usage-statistics-collector-daemon", "flask-login-extension-name",
    "A line about a secret, a password and a token. Nothing else.",
])
def test_secret_detector_ignores_placeholders_and_known_fakes(ps, text):
    assert ps.scan_secrets(text + "\n") == [], text


def test_the_allow_pragma_silences_a_secret_but_not_a_personal_term(ps):
    line = f"x = '{GH_PAT}'  # privacy-scan: allow"
    assert ps.scan_secrets(line) == []
    assert ps.TermList.parse(NAME).find(f"{NAME}  # privacy-scan: allow") == [(1, 1)]


# ---------------------------------------------------------------- the list of terms

def test_missing_list_local_is_a_warning_and_exit_zero(tmp_path):
    write(tmp_path / "t" / "a.txt", "nothing here\n")
    code, out = scan(tmp_path, "--tree", "t")
    assert code == 0
    assert "warning" in out.lower() and "personal-terms" in out


def test_missing_list_in_ci_on_main_is_exit_two(tmp_path):
    write(tmp_path / "t" / "a.txt", "nothing here\n")
    code, out = scan(tmp_path, "--ci", "--tree", "t", GITHUB_REF="refs/heads/main")
    assert code == 2
    assert "PERSONAL_TERMS" in out        # tells the owner which secret is missing


@pytest.mark.parametrize("ref", ["refs/pull/7/merge", "refs/heads/some-branch", ""])
def test_missing_list_in_ci_elsewhere_is_only_a_warning(tmp_path, ref):
    """Pull requests from forks do not get the secret."""
    write(tmp_path / "t" / "a.txt", "nothing here\n")
    code, out = scan(tmp_path, "--ci", "--tree", "t", GITHUB_REF=ref)
    assert code == 0 and "warning" in out.lower()


def test_terms_from_the_env_file_variable_beat_the_env_list(tmp_path):
    f = write(tmp_path / "terms.txt", f"# mine\n{PLACE}\n")
    write(tmp_path / "t" / "a.txt", f"about {NAME}\nabout {PLACE}\n")
    code, out = scan(tmp_path, "--tree", "t", terms=[NAME], KAIRO_PERSONAL_TERMS_FILE=str(f))
    assert code == 1
    assert "a.txt:2: personal term #1" in out and "a.txt:1" not in out          # the file's list was used
    no_leak(out, NAME, PLACE)


def test_terms_list_in_the_data_folder_and_in_the_home_config(tmp_path):
    write(tmp_path / "t" / "a.txt", f"about {PLACE}\n")
    write(tmp_path / "no-data" / "config" / "personal-terms.txt", PLACE + "\n")
    code, out = scan(tmp_path, "--tree", "t")
    assert code == 1 and "a.txt:1: personal term #1" in out
    shutil.rmtree(tmp_path / "no-data")
    write(tmp_path / "home" / ".config" / "kairo" / "personal-terms.txt", PLACE + "\n")
    code, out = scan(tmp_path, "--tree", "t")
    assert code == 1 and "a.txt:1: personal term #1" in out


def test_a_terms_file_named_on_purpose_that_is_missing_is_an_error(tmp_path):
    """A typo must not switch the check off (build-release.sh, the hooks)."""
    write(tmp_path / "t" / "a.txt", f"about {PLACE}\n")
    code, out = scan(tmp_path, "--tree", "t", terms=[PLACE], KAIRO_PERSONAL_TERMS_FILE=str(tmp_path / "nope.txt"))
    assert code == 2 and "does not exist" in out and "clean" not in out
    code, out = scan(tmp_path, "--tree", "t", "--terms-file", str(tmp_path / "nope.txt"))
    assert code == 2 and "does not exist" in out
    empty = write(tmp_path / "empty.txt", "# nothing yet\n")
    code, out = scan(tmp_path, "--tree", "t", "--terms-file", str(empty))
    assert code == 2 and "no terms" in out


def test_an_empty_list_counts_as_no_list(tmp_path):
    write(tmp_path / "t" / "a.txt", "x\n")
    code, out = scan(tmp_path, "--ci", "--tree", "t", terms=["# nothing yet", ""], GITHUB_REF="refs/heads/main")
    assert code == 2


def test_a_bad_regex_in_the_list_is_exit_two_and_never_printed(tmp_path):
    write(tmp_path / "t" / "a.txt", "x\n")
    code, out = scan(tmp_path, "--tree", "t", terms=[NAME, f"regex:{ZORB}("])
    assert code == 2 and "personal term #2" in out
    no_leak(out, ZORB, NAME)


def test_the_rule_number_in_the_report_counts_rules_and_names_the_list_line(tmp_path):
    write(tmp_path / "t" / "a.txt", f"about {NAME}\n")
    code, out = scan(tmp_path, "--tree", "t", terms=["# comment", PLACE, NAME])
    assert code == 1 and "a.txt:1: personal term #2 (list line 3)" in out


# ---------------------------------------------------------------- tree

def test_tree_finds_terms_and_never_prints_them(tmp_path):
    write(tmp_path / "t" / "a.txt", "clean\n")
    write(tmp_path / "t" / "deep" / "er" / "b.md", f"line one\nline two {NAME.upper()}\nline {UML.lower()} three\n")
    write(tmp_path / "t" / "c.py", f"x = 1  # {PLACE}\n")
    code, out = scan(tmp_path, "--tree", "t", terms=[NAME, PLACE, UML])
    assert code == 1
    assert "deep/er/b.md:2: personal term #1" in out
    assert "deep/er/b.md:3: personal term #3" in out
    assert "c.py:1: personal term #2" in out
    assert "a.txt" not in out
    no_leak(out, NAME, PLACE, UML, UML_ASCII)


def test_tree_clean_is_exit_zero(tmp_path):
    write(tmp_path / "t" / "a.txt", "nothing personal here, only Jordanian-style words\n")
    code, out = scan(tmp_path, "--tree", "t", terms=[NAME, PLACE])
    assert code == 0 and "clean" in out


def test_tree_flags_a_term_in_a_file_name_without_printing_the_name(tmp_path):
    write(tmp_path / "t" / f"cv-{NAME.replace(' ', '-')}.txt", "hello\n")
    write(tmp_path / "t" / "ok.txt", "hello\n")
    code, out = scan(tmp_path, "--tree", "t", terms=[NAME])
    assert code == 1 and "personal term #1" in out and "ok.txt" not in out
    no_leak(out, NAME, NAME.replace(" ", "-"), "testperson")


def test_tree_flags_forbidden_paths_anywhere(tmp_path):
    for rel in ["private/p.md", "a/data/x.txt", "devdata/y", "app.local.yaml", ".env", "db/x.sqlite", "ok/.env.example"]:
        write(tmp_path / "t" / rel, "x\n")
    code, out = scan(tmp_path, "--tree", "t", terms=[NAME])
    assert code == 1
    for rel in ["private/p.md", "a/data/x.txt", "devdata/y", "app.local.yaml", ".env", "db/x.sqlite"]:
        assert f"{rel}: forbidden path" in out, rel
    assert ".env.example" not in out


def test_tree_reads_the_strings_of_binary_files_whatever_their_extension(tmp_path):
    write(tmp_path / "t" / "store.dat", b"\x00\x01\x02 " + NAME.encode() + b" \x00\xff\x00")
    write(tmp_path / "t" / "pic.png", b"\x89PNG\r\n\x1a\n\x00" + NAME.encode() + b"\x00")      # not a real PNG
    write(tmp_path / "t" / "clean.png", png_with_text(b"Comment", b"nothing personal here"))
    code, out = scan(tmp_path, "--tree", "t", terms=[NAME])
    assert code == 1 and "store.dat" in out and "pic.png" in out and "clean.png" not in out
    no_leak(out, NAME)


def test_tree_looks_inside_office_files_and_zips(tmp_path):
    z = tmp_path / "t" / "letter.docx"
    z.parent.mkdir()
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", f"<w:t>{NAME}</w:t>")
    code, out = scan(tmp_path, "--tree", "t", terms=[NAME])
    assert code == 1 and "letter.docx!/word/document.xml:1: personal term #1" in out
    no_leak(out, NAME)


def test_tree_in_a_git_repo_skips_what_git_ignores(tmp_path):
    repo = make_repo(tmp_path)
    write(repo / ".gitignore", "ignored/\n.env\n")
    write(repo / "ignored" / "x.txt", f"{NAME}\n")
    write(repo / ".env", "A=1\n")
    write(repo / "new.txt", f"{NAME}\n")             # not added yet: still scanned
    code, out = scan(tmp_path, "--tree", ".", terms=[NAME], cwd=repo)
    assert code == 1 and "new.txt:1" in out and "ignored" not in out and ".env" not in out


def test_tree_outside_git_scans_everything_except_dot_git(tmp_path):
    write(tmp_path / "t" / ".env", "A=1\n")
    write(tmp_path / "t" / ".git" / "config", f"{NAME}\n")
    code, out = scan(tmp_path, "--tree", "t", terms=[NAME])
    assert code == 1 and ".env: forbidden path" in out and ".git/config" not in out


def test_tree_flags_secrets_with_file_and_line_and_without_the_value(tmp_path):
    write(tmp_path / "t" / "cfg.txt", f"ok\nkey = {GH_PAT}\n")
    write(tmp_path / "t" / "k.pem", PEM_RSA + "\n")
    code, out = scan(tmp_path, "--tree", "t", terms=[NAME])
    assert code == 1
    assert "cfg.txt:2: secret: GitHub token" in out and "k.pem:1: secret: private key" in out
    no_leak(out, GH_PAT, GH_PAT[10:], "MIIBOgIBAAJBAKj34")


def test_files_with_the_allow_pragma_pass_the_secret_check_only_where_fakes_live(tmp_path):
    write(tmp_path / "t" / "server" / "tests" / "cfg.txt", f"key = {GH_PAT}  # privacy-scan: allow\n")
    write(tmp_path / "t" / "docs" / "cfg.md", f"key = {GH_PAT}  # privacy-scan: allow\n")
    code, out = scan(tmp_path, "--tree", "t", terms=[NAME])
    assert code == 0, out
    # a pull request cannot excuse a real secret anywhere else
    write(tmp_path / "t" / "server" / "app" / "cfg.py", f"key = {GH_PAT}  # privacy-scan: allow\n")
    code, out = scan(tmp_path, "--tree", "t", terms=[NAME])
    assert code == 1 and "server/app/cfg.py:1: secret: GitHub token" in out


# ---------------------------------------------------------------- staged

def test_staged_flags_added_lines_with_file_and_line(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, tmp_path, "start", {"notes.txt": "fine\n"})
    write(repo / "notes.txt", f"fine\nmet {NAME} today\n")
    write(repo / "new.md", f"{PLACE}\n")
    write(repo / "unstaged.txt", f"{NAME}\n")
    stage(repo, tmp_path, "notes.txt", "new.md")
    code, out = scan(tmp_path, "--staged", terms=[PLACE, NAME], cwd=repo)
    assert code == 1
    assert "notes.txt:2: personal term #2" in out and "new.md:1: personal term #1" in out
    assert "unstaged" not in out
    no_leak(out, NAME, PLACE)


def test_staged_only_looks_at_what_the_commit_adds(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, tmp_path, "old", {"a.txt": f"{NAME}\nline\n"})
    write(repo / "a.txt", f"{NAME}\nline\nmore\n")
    stage(repo, tmp_path, "a.txt")
    code, out = scan(tmp_path, "--staged", terms=[NAME], cwd=repo)
    assert code == 0, out                          # the old line is not part of this commit


def test_staged_reads_the_index_not_the_working_copy(tmp_path):
    repo = make_repo(tmp_path)
    write(repo / "a.txt", f"{NAME}\n")
    stage(repo, tmp_path, "a.txt")
    write(repo / "a.txt", "scrubbed after staging\n")
    code, out = scan(tmp_path, "--staged", terms=[NAME], cwd=repo)
    assert code == 1 and "a.txt:1" in out


def test_staged_flags_forbidden_paths_and_secrets_and_binary_files(tmp_path):
    repo = make_repo(tmp_path)
    write(repo / "private" / "profile.md", "hello\n")
    write(repo / ".env", "A=1\n")
    write(repo / ".env.example", "A=\n")
    write(repo / "cfg.txt", f"t = {GH_PAT}\n")
    write(repo / "blob.dat", b"\x00\x01" + NAME.encode() + b"\x00")
    stage(repo, tmp_path, "private/profile.md", ".env", ".env.example", "cfg.txt", "blob.dat")
    code, out = scan(tmp_path, "--staged", terms=[NAME], cwd=repo)
    assert code == 1
    assert "private/profile.md: forbidden path" in out and ".env: forbidden path" in out
    assert "cfg.txt:1: secret: GitHub token" in out and "blob.dat" in out
    assert ".env.example" not in out
    no_leak(out, GH_PAT, NAME)


def test_staged_with_nothing_staged_or_only_deletions_is_clean(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, tmp_path, "start", {"a.txt": f"{NAME}\n", ".env": "A=1\n"})
    code, out = scan(tmp_path, "--staged", terms=[NAME], cwd=repo)
    assert code == 0
    git(repo, tmp_path, "rm", "-q", "a.txt", ".env")
    code, out = scan(tmp_path, "--staged", terms=[NAME], cwd=repo)
    assert code == 0, out


def test_staged_copes_with_odd_file_names(tmp_path):
    repo = make_repo(tmp_path)
    write(repo / "dir with space" / "füll näme.txt", f"x\n{NAME}\n")
    stage(repo, tmp_path, "dir with space/füll näme.txt")
    code, out = scan(tmp_path, "--staged", terms=[NAME], cwd=repo)
    assert code == 1 and "dir with space/füll näme.txt:2: personal term #1" in out


# ---------------------------------------------------------------- history

def history_repo(tmp_path: Path) -> Path:
    """HEAD is clean; the past is not."""
    repo = make_repo(tmp_path)
    commit(repo, tmp_path, "first", {"README.md": "hello\n"})
    commit(repo, tmp_path, "add notes", {"old/notes.txt": f"a\nb {NAME}\n"})
    commit(repo, tmp_path, "remove notes again", remove=["old/notes.txt"])
    return repo


def test_history_finds_what_was_removed_later(tmp_path):
    repo = history_repo(tmp_path)
    assert scan(tmp_path, "--tree", ".", terms=[NAME], cwd=repo)[0] == 0          # the files today are clean
    code, out = scan(tmp_path, "--history", terms=[NAME], cwd=repo)
    assert code == 1
    assert "old/notes.txt:2: personal term #1" in out and "blob" in out
    no_leak(out, NAME)


def test_history_scans_commit_messages_and_identities(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, tmp_path, "subject line\n\nbody names " + NAME, {"a.txt": "x\n"})
    commit(repo, tmp_path, "innocent", {"b.txt": "y\n"}, GIT_AUTHOR_EMAIL=f"{PLACE.lower()}@example.org")
    commit(repo, tmp_path, "innocent too", {"c.txt": "z\n"}, GIT_COMMITTER_NAME=NAME)
    code, out = scan(tmp_path, "--history", terms=[NAME, PLACE], cwd=repo)
    assert code == 1
    assert "message:3: personal term #1" in out
    assert "author email: personal term #2" in out
    assert "committer name: personal term #1" in out
    no_leak(out, NAME, PLACE)


def test_history_checks_the_given_refs_only(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, tmp_path, "base", {"a.txt": "x\n"})
    git(repo, tmp_path, "switch", "-q", "-c", "side")
    commit(repo, tmp_path, "side work", {"s.txt": f"{PLACE}\n"})
    git(repo, tmp_path, "switch", "-q", "main")
    assert scan(tmp_path, "--history", "main", terms=[PLACE], cwd=repo)[0] == 0
    code, out = scan(tmp_path, "--history", "side", terms=[PLACE], cwd=repo)
    assert code == 1 and "s.txt:1" in out
    assert scan(tmp_path, "--history", terms=[PLACE], cwd=repo)[0] == 1           # default: every ref


def test_history_flags_forbidden_paths_and_secrets_that_were_deleted(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, tmp_path, "oops", {".env": "A=1\n", "private/me.md": "hi\n", "cfg.txt": f"t = {GH_PAT}\n"})
    commit(repo, tmp_path, "cleanup", remove=[".env", "private/me.md", "cfg.txt"])
    code, out = scan(tmp_path, "--history", terms=[NAME], cwd=repo)
    assert code == 1
    assert ".env: forbidden path" in out and "private/me.md: forbidden path" in out
    assert "cfg.txt:1: secret: GitHub token" in out
    no_leak(out, GH_PAT)


def test_history_clean_is_exit_zero(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, tmp_path, "one", {"a.txt": "hello\n"})
    commit(repo, tmp_path, "two", {"a.txt": "hello again\n"})
    code, out = scan(tmp_path, "--history", terms=[NAME], cwd=repo)
    assert code == 0 and "clean" in out


def test_history_names_the_commit_that_brought_it_in(tmp_path):
    repo = history_repo(tmp_path)
    sha = git(repo, tmp_path, "log", "--format=%h", "--grep=add notes").strip()
    code, out = scan(tmp_path, "--history", terms=[NAME], cwd=repo)
    assert code == 1 and sha in out


def test_history_in_a_shallow_clone_is_an_error_in_ci(tmp_path):
    repo = history_repo(tmp_path)
    clone = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", repo.as_uri(), str(clone)], check=True,
                   env=clean_env(tmp_path), capture_output=True)
    code, out = scan(tmp_path, "--ci", "--history", terms=[NAME], cwd=clone, GITHUB_REF="refs/heads/main")
    assert code == 2 and "shallow" in out.lower()
    code, out = scan(tmp_path, "--history", terms=[NAME], cwd=clone)             # locally only a warning
    assert code == 0 and "shallow" in out.lower()


def test_history_scans_the_ref_names_too(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, tmp_path, "base", {"a.txt": "x\n"})
    git(repo, tmp_path, "branch", f"fix-{PLACE.lower()}")
    code, out = scan(tmp_path, "--history", terms=[PLACE], cwd=repo)
    assert code == 1 and "personal term #1" in out
    no_leak(out, PLACE)


# ---------------------------------------------------------------- zip

def make_zip(path: Path, files: dict[str, str | bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return path


def test_zip_finds_terms_secrets_and_forbidden_paths(tmp_path):
    z = make_zip(tmp_path / "Kairo-Setup-1.zip", {
        "Kairo-Setup-1/app/config/profile.yaml": f"name: x\nowner: {NAME}\n",
        "Kairo-Setup-1/app/private/profile.md": "hello\n",
        "Kairo-Setup-1/app/data/db/kairo.sqlite": b"SQLite format 3\x00",
        "Kairo-Setup-1/app/.env": "A=1\n",
        "Kairo-Setup-1/app/server/cfg.py": f"x = '{GH_PAT}'\n",
        "Kairo-Setup-1/app/README.md": "fine\n",
    })
    code, out = scan(tmp_path, "--zip", str(z), terms=[NAME])
    assert code == 1
    assert "app/config/profile.yaml:2: personal term #1" in out
    for rel in ["app/private/profile.md", "app/data/db/kairo.sqlite", "app/.env"]:
        assert f"{rel}: forbidden path" in out, rel
    assert "app/server/cfg.py:1: secret: GitHub token" in out
    assert "README.md" not in out
    no_leak(out, NAME, GH_PAT)


def test_zip_looks_into_zips_inside_zips(tmp_path):
    inner = tmp_path / "inner.zip"
    make_zip(inner, {"x/notes.txt": f"{PLACE}\n"})
    z = make_zip(tmp_path / "outer.zip", {"pkg/inner.zip": inner.read_bytes()})
    code, out = scan(tmp_path, "--zip", str(z), terms=[PLACE])
    assert code == 1 and "pkg/inner.zip!/x/notes.txt:1: personal term #1" in out


def test_zip_clean_is_exit_zero(tmp_path):
    z = make_zip(tmp_path / "ok.zip", {"Kairo-Setup-1/app/config/profile.yaml": "name: Owner\ngmail: owner@example.com\n",
                                       "Kairo-Setup-1/app/web/dist/index.html": "<html></html>"})
    code, out = scan(tmp_path, "--zip", str(z), terms=[NAME, PLACE])
    assert code == 0 and "clean" in out


def test_zip_that_is_not_a_zip_is_an_error(tmp_path):
    write(tmp_path / "bad.zip", "not a zip")
    code, out = scan(tmp_path, "--zip", str(tmp_path / "bad.zip"), terms=[NAME])
    assert code == 2
    code, out = scan(tmp_path, "--zip", str(tmp_path / "missing.zip"), terms=[NAME])
    assert code == 2


# ---------------------------------------------------------------- the CI face

def test_ci_mode_writes_github_annotations_without_the_term(tmp_path):
    write(tmp_path / "t" / "a.txt", f"x\n{NAME}\n")
    code, out = scan(tmp_path, "--ci", "--tree", "t", terms=[NAME], GITHUB_ACTIONS="true", GITHUB_REF="refs/heads/main")
    assert code == 1
    assert "::error file=" in out and "line=2" in out
    assert "%3A" not in out and "%2C" not in out          # only the file= property is escaped that way
    no_leak(out, NAME)


def test_modes_can_be_combined_and_one_is_needed(tmp_path):
    repo = history_repo(tmp_path)
    code, out = scan(tmp_path, "--history", "--tree", ".", terms=[NAME], cwd=repo)
    assert code == 1
    code, out = scan(tmp_path, terms=[NAME], cwd=repo)
    assert code == 2


# ---------------------------------------------------------------- the real repo

def test_the_repo_itself_is_clean_with_a_made_up_list(tmp_path):
    """The owner's rule 0.6 in one test: the tree as it is, with made-up terms, finds nothing. Fake keys in the
    tests, placeholders in the docs and the scanner's own patterns must not trip the scanner."""
    code, out = scan(tmp_path, "--tree", str(REPO_ROOT), terms=[NAME, PLACE, UML, STRASSE, "regex:" + ZORB + r"\d+"],
                     cwd=REPO_ROOT)
    assert code == 0, out
    assert "not scanned" not in out


def test_the_repos_own_fake_test_secrets_are_not_flagged(ps):
    offenders = []
    for p in (REPO_ROOT / "server" / "tests").rglob("*.py"):
        for line, rule in ps.scan_secrets(p.read_text(encoding="utf-8")):
            offenders.append(f"{p.name}:{line}: {rule}")
    assert offenders == []


# ======================================================================================================
# Hardening after the adversarial review. Every hole the attackers found has a test below (made-up data only;
# every name is built from pieces or at run time, so that this file stays clean for the scan of the whole repo).
# ======================================================================================================

REVERSED = ", ".join(reversed(NAME.split()))                  # "Last, First"
PHONE = j("079 555 ", "12 34")                                # made up (555)
IBAN = j("CH44 3199 9123 ", "0008 8901 2")                    # the example IBAN of the standard
U = chr(0xFC)                                                 # the u-umlaut, written as a number so no escape is in the source


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def hit(tmp_path: Path, content: str | bytes, name: str = "a.txt", terms: list[str] | None = None, **env) -> tuple[int, str]:
    """Scan one file with --tree."""
    d = tmp_path / "t"
    if d.exists():
        shutil.rmtree(d)
    write(d / name, content)
    return scan(tmp_path, "--tree", "t", terms=terms if terms is not None else [NAME, UML, PLACE], **env)


# text -> (rule number in [NAME, UML, PLACE], line of the hit)
ENCODED: dict[str, tuple[str | bytes, int, int]] = {
    "json-ascii": (json.dumps({"street": UML + " 5"}), 2, 1),
    "json-escaped-space": (json.dumps({"n": NAME}).replace(" ", "\\u0020"), 1, 1),
    "json-literal-backslash-n-between-words": (json.dumps({"n": NAME.replace(" ", "\n")}), 1, 1),
    "js-hex-escape": ('var s = "' + UML.replace(U, "\\xfc") + '";', 2, 1),
    "js-utf8-bytes": ('var s = "' + "".join(f"\\x{b:02x}" for b in UML.encode()) + '";', 2, 1),
    "js-brace-escape": ("var s = '" + UML.replace(U, "\\u{fc}") + "';", 2, 1),
    "python-N-escape": ('s = "' + UML.replace(U, "\\N{LATIN SMALL LETTER U WITH DIAERESIS}") + '"', 2, 1),
    "surrogate-pair": ("x\n" + json.dumps({"n": NAME[:3] + chr(0x1F600) + NAME[3:]}).replace(chr(0x1F600), "") + "\n" + json.dumps(NAME + chr(0x1F600)), 1, 2),
    "url-utf8": ("https://x.example/?q=" + urllib.parse.quote(UML), 2, 1),
    "url-space": ("https://x.example/?q=" + urllib.parse.quote(NAME), 1, 1),
    "url-plus": ("https://x.example/?q=" + NAME.replace(" ", "+"), 1, 1),
    "html-named": ("<p>" + UML.replace(U, "&uuml;") + "</p>", 2, 1),
    "html-decimal": ("<p>" + UML.replace(U, "&#252;") + "</p>", 2, 1),
    "html-hex": ("<p>" + UML.replace(U, "&#xFC;") + "</p>", 2, 1),
    "html-nbsp": ("<p>" + NAME.replace(" ", "&nbsp;") + "</p>", 1, 1),
    "quoted-printable": (quopri.encodestring(UML.encode()).decode(), 2, 1),
    "quoted-printable-soft-line-break": ("ok\n" + quopri.encodestring(UML.encode()).decode().replace("hausen", "=\nhausen"), 2, 2),
    "rfc2047-base64": ("From: =?utf-8?B?" + b64(UML) + "?= <a@example.org>", 2, 1),
    "rfc2047-q": ("Subject: =?utf-8?Q?" + quopri.encodestring(UML.encode(), header=True).decode().strip() + "?=", 2, 1),
    "rfc2047-one-word-each": ("From: " + " ".join(f"=?utf-8?B?{b64(w)}?=" for w in NAME.split()) + " <a@x.example>", 1, 1),
    "base64-in-json": (json.dumps({"blob": b64(f"{NAME}, {PLACE}")}), 1, 1),
    "base64-url-safe-no-padding": (json.dumps({"blob": base64.urlsafe_b64encode(UML.encode()).decode().rstrip("=")}), 2, 1),
    "base64-twice": (b64(b64(NAME)), 1, 1),
    "url-inside-json": (json.dumps({"u": urllib.parse.quote(NAME)}), 1, 1),
    "last-comma-first": (REVERSED, 1, 1),
    "csv-last-first": (f"Name;Vorname\n{';'.join(reversed(NAME.split()))}\n", 1, 2),
    "words-run-together": (NAME.replace(" ", ""), 1, 1),
    "camel-case-file-name-stem": ("see " + "".join(w.capitalize() for w in NAME.split()) + ".md", 1, 1),
    "email-local-part": (NAME.replace(" ", "").lower() + "@example.org", 1, 1),
    "email-dotted-and-underscored": (NAME.replace(" ", ".").lower() + "@example.org\n" + NAME.replace(" ", "_") + "@x.example", 1, 1),
    "umlaut-written-as-plain-u": ("a " + UML.replace(U, "u"), 2, 1),
    "umlaut-written-as-ue": ("a " + UML.replace(U, "ue"), 2, 1),
    "umlaut-decomposed": ("a " + UML.replace(U, "u" + chr(0x308)), 2, 1),
    "soft-hyphen-inside-a-word": (PLACE[:4] + chr(0xAD) + PLACE[4:], 3, 1),
    "zero-width-space-inside-a-word": (PLACE[:4] + chr(0x200B) + PLACE[4:], 3, 1),
    "fullwidth-letters": (PLACE.translate({ord(c): ord(c) + 0xFEE0 for c in "abcdefghijklmnopqrstuvwxyz"}), 3, 1),
    "cyrillic-look-alike-letter": (NAME.replace("o", chr(0x43E)), 1, 1),
    "utf-16-le-without-bom": (NAME.encode("utf-16-le") + "\n".encode("utf-16-le"), 1, 1),
    "utf-16-le-with-bom": (NAME.encode("utf-16"), 1, 1),
    "utf-32-with-bom": (NAME.encode("utf-32"), 1, 1),
    "utf-16-be-without-bom": ("x\n".encode("utf-16-be") + NAME.encode("utf-16-be"), 1, 2),
    "utf-32-le-without-bom": ((NAME + "\n").encode("utf-32-le"), 1, 1),
    "utf-32-be-without-bom": (("x\n" + NAME).encode("utf-32-be"), 1, 2),
    "tag-character-inside-a-word": (PLACE[:4] + chr(0xE0041) + PLACE[4:], 3, 1),
    "variation-selector-supplement-inside-a-word": (PLACE[:4] + chr(0xE0100) + PLACE[4:], 3, 1),
    "arabic-format-character-inside-a-word": (PLACE[:4] + chr(0x600) + PLACE[4:], 3, 1),
    "braille-blank-inside-a-word": (PLACE[:4] + chr(0x2800) + PLACE[4:], 3, 1),
    "c1-control-inside-a-word": ("x " + PLACE[:4] + chr(0x80) + PLACE[4:], 3, 1),
    "delete-inside-a-word": (PLACE[:4] + chr(0x7F) + PLACE[4:], 3, 1),
    "ascii-control-inside-a-word": (PLACE[:4] + chr(0x1) + PLACE[4:], 3, 1),
    "ascii-control-between-words": ("field" + chr(0x1) + NAME, 1, 1),
    "latin-1-bytes": (UML.encode("latin-1"), 2, 1),
}


@pytest.mark.parametrize("label", sorted(ENCODED))
def test_every_spelling_of_a_term_is_found(tmp_path, label):
    content, rule_no, line = ENCODED[label]
    code, out = hit(tmp_path, content)
    assert code == 1 and f"a.txt:{line}: personal term #{rule_no}" in out, out
    no_leak(out, NAME, UML, PLACE)


@pytest.mark.parametrize("label", ["json-ascii", "html-named", "words-run-together", "last-comma-first", "quoted-printable"])
def test_the_spellings_are_found_in_the_other_modes_too(tmp_path, label):
    content, rule_no, _line = ENCODED[label]
    terms = [NAME, UML, PLACE]
    # --staged
    repo = make_repo(tmp_path)
    write(repo / "a.txt", content)
    stage(repo, tmp_path, "a.txt")
    code, out = scan(tmp_path, "--staged", terms=terms, cwd=repo)
    assert code == 1 and f"a.txt:1: personal term #{rule_no}" in out, out
    # --history (the file is gone at HEAD)
    commit(repo, tmp_path, "add")
    commit(repo, tmp_path, "remove", remove=["a.txt"])
    code, out = scan(tmp_path, "--history", terms=terms, cwd=repo)
    assert code == 1 and f"a.txt:1: personal term #{rule_no}" in out, out
    # --zip, in a source map of the web build
    z = make_zip(tmp_path / "rel.zip", {"Kairo-Setup-1/app/web/dist/assets/index.js.map": content if isinstance(content, bytes) else content.encode()})
    code, out = scan(tmp_path, "--zip", str(z), terms=terms)
    assert code == 1 and f"index.js.map:1: personal term #{rule_no}" in out, out
    no_leak(out, NAME, UML, PLACE)


def test_a_source_map_with_the_sharp_s_spelled_both_ways_in_a_release_zip(tmp_path):
    z = make_zip(tmp_path / "rel.zip", {"Kairo-Setup-1/app/web/dist/assets/index.js.map":
                                       json.dumps({"sourcesContent": ["const a = " + repr(STRASSE)]})})
    code, out = scan(tmp_path, "--zip", str(z), terms=[STRASSE])
    assert code == 1 and "index.js.map:1: personal term #1" in out


def test_decoding_and_looser_matching_do_not_turn_ordinary_text_into_hits(tmp_path):
    ordinary = "\n".join([
        'print("a\\nb\\tc \\x00 100%% &amp; =3D x%20y")', "color: #fff; width: 50%; margin: 0 auto;",
        "a=b&c=d&e=f; q=1;", "Jordanian Testpersonal", j("Example", "towns and the Example", "township"), "kodak mueller",
        "see https://example.org/path/%7Euser/file.txt and mailto:someone@example.org", "=?utf-8?B?aGVsbG8gd29ybGQ=?=",
    ])
    code, out = hit(tmp_path, ordinary, terms=[NAME, UML, PLACE])
    assert code == 0, out


def test_a_term_with_digits_matches_any_grouping_and_the_country_code(tmp_path):
    lines = ["call " + x for x in ("0795551234", "079 555 1234", "079-555-12-34", "+41 79 555 12 34", "0041795551234",
                                   "+41 (0) 79 555 12 34".replace("(0) ", ""))]
    lines += ["IBAN CH4431999123000889012", "IBAN ch44-3199-9123-0008-8901-2"]
    code, out = hit(tmp_path, "\n".join(lines) + "\n", terms=[PHONE, IBAN])
    assert code == 1
    for n in range(1, 9):
        assert f"a.txt:{n}: personal term #{1 if n <= 6 else 2}" in out, (n, out)
    no_leak(out, PHONE, "5551234", "3199")


def test_numbers_keep_their_edges_against_other_digits_only(tmp_path):
    digits = PHONE.replace(" ", "")
    code, out = hit(tmp_path, "id 1" + digits + "\nid " + digits + "5\nref 12" + PHONE + "\n", terms=[PHONE])
    assert code == 0, out
    code, out = hit(tmp_path, "tel" + digits + "\n" + digits + "ext\nID" + PHONE + "\n", terms=[PHONE])       # letters glued to a number are fine
    assert code == 1 and out.count("a.txt:") == 3, out


def test_a_single_name_still_needs_its_word_edges_in_every_spelling(tmp_path):
    code, out = hit(tmp_path, "Jordanian\nTestpersonal\nmyjordan\nunTestperson\n", terms=[NAME.split()[0], NAME.split()[1]])
    assert code == 0, out


# ---------------------------------------------------------------- the files the old scanner never read

def test_png_chunks_wav_tags_and_pdf_metadata_are_read(tmp_path):
    d = tmp_path / "t"
    write(d / "a.png", png_with_text(b"Comment", NAME.encode()))
    write(d / "b.png", png_with_text(b"Comment", NAME.encode(), compressed=True))
    write(d / "c.wav", wav_with_info(NAME.encode()))
    write(d / "d.dat", pdf_with(NAME))
    write(d / "e.dat", pdf_with(None, b"BT /F1 12 Tf [(Jor)-20(dan)-250(Test)-5(person)] TJ ET"))
    write(d / "f.dat", pdf_with(None, b"BT (" + NAME.encode() + b") Tj ET"))
    write(d / "g.dat", pdf_with(None, b"BT [<FEFF" + NAME.encode("utf-16-be").hex().upper().encode() + b">] TJ ET"))
    write(d / "h.png", b"\x00" + NAME.encode() + b" lives in " + PLACE.encode() + b"\n")          # text renamed .png, NUL first
    write(d / "i.dat", b"\x00\x01" + UML.encode("latin-1") + b"\x00")                               # latin-1 bytes in a binary
    write(d / "j.dat", b"\x00\x01" + NAME.encode("utf-16-le") + b"\x00\x00")                        # UTF-16 inside a binary
    write(d / "k.dat", b"\x00\x01\x02" + b"\x89\xfe\xdd" * 300 + b"nothing personal in this binary" + b"\x00\xff")
    code, out = scan(tmp_path, "--tree", "t", terms=[NAME, UML, PLACE])
    assert code == 1, out
    import re
    for f in "abcdefghij":
        ext = "png" if f in "abh" else "wav" if f == "c" else "dat"
        assert re.search(rf"^{f}\.{ext}(:\d+)?: personal term #", out, re.M), (f, out)
    assert "k.dat" not in out
    no_leak(out, NAME, UML, PLACE)


def test_a_pdf_or_a_photo_is_not_allowed_outside_the_icon_folder(tmp_path):
    for rel in ("tests/fixtures/letter.pdf", "docs/photo.JPG", "x/scan.heic", "x/sheet.xlsx", "x/mail.pst", "x/a.docx"):
        write(tmp_path / "t" / rel, "x\n")
    write(tmp_path / "t" / "web" / "public" / "paper.pdf", "x\n")
    code, out = scan(tmp_path, "--tree", "t", terms=[NAME])
    assert code == 1
    for rel in ("tests/fixtures/letter.pdf", "docs/photo.JPG", "x/scan.heic", "x/sheet.xlsx", "x/mail.pst", "x/a.docx"):
        assert f"{rel}: forbidden path: document or photo file" in out, rel
    assert "web/public/paper.pdf" not in out


def test_a_database_is_found_by_its_header_and_a_secret_store_by_its_magic(tmp_path):
    d = tmp_path / "t"
    write(d / "fixtures" / "kairo", b"SQLite format 3\x00" + b"\x00" * 100)
    secrets_json = json.dumps({"token": j("ya", "29.", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4Y5z6"),
                               "client_secret": GOCSPX})
    write(d / "fixtures" / "google.dat", b"KAIRO-DEV1\n" + base64.b64encode(secrets_json.encode()))
    write(d / "fixtures" / "win.dat", b"KAIRO-DPAPI1\n" + b"\x01\x00\x00\x00\xd0\x8c\x9d\xdf")
    write(d / "fixtures" / "plain.dat", b"hello\n")
    code, out = scan(tmp_path, "--tree", "t", terms=[NAME])
    assert code == 1
    assert "kairo: forbidden content: SQLite database" in out
    assert "google.dat: forbidden content: Kairo secret store" in out and "win.dat: forbidden content: Kairo secret store" in out
    assert "google.dat:1: secret: Google OAuth token" in out or "google.dat:2: secret: Google OAuth token" in out   # decoded base64
    assert "plain.dat" not in out
    no_leak(out, GOCSPX)


# ---------------------------------------------------------------- archives

TEXT = f"hello\nmet {NAME} at {PLACE}\n"


def zbytes(entries: dict[str, str | bytes]) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for n, c in entries.items():
            z.writestr(n, c)
    return b.getvalue()


def tar_bytes(entries: dict[str, bytes], mode: str = "w") -> bytes:
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode=mode) as t:
        for n, c in entries.items():
            info = tarfile.TarInfo(n)
            info.size = len(c)
            t.addfile(info, io.BytesIO(c))
    return b.getvalue()


def nested_zip(levels: int, payload: dict[str, str | bytes]) -> bytes:
    data = zbytes(payload)
    for i in range(levels - 1):
        data = zbytes({f"level{i}.zip": data})
    return data


ARCHIVES: dict[str, tuple[str, bytes]] = {
    "gzip": ("app/notes.txt.gz", gzip.compress(TEXT.encode())),
    "tar.gz": ("app/bundle.tar.gz", tar_bytes({"private/seed.md": TEXT.encode()}, "w:gz")),
    "tgz": ("app/bundle.tgz", tar_bytes({"seed.md": TEXT.encode()}, "w:gz")),
    "plain tar": ("app/bundle.tar", tar_bytes({"seed.md": TEXT.encode()})),
    "bzip2": ("app/notes.txt.bz2", bz2.compress(TEXT.encode())),
    "xz": ("app/notes.txt.xz", lzma.compress(TEXT.encode())),
    "gzip without a name that says so": ("app/blob.bin", gzip.compress(TEXT.encode())),
    "zip with a byte in front": ("app/inner.bin", b"X" + zbytes({"notes.txt": TEXT})),
    "zip behind an installer stub": ("app/setup.exe", b"MZ" + b"\x90" * 600 + zbytes({"notes.txt": TEXT})),
    "zip in zip, six deep": ("app/inner.zip", nested_zip(6, {"notes.txt": TEXT})),
    "office file": ("app/letter.docx", zbytes({"word/document.xml": f"<w:t>{NAME}</w:t>"})),
    "gzip inside a tar inside a zip": ("app/x.zip", zbytes({"y.tar": tar_bytes({"z.txt.gz": gzip.compress(TEXT.encode())})})),
}


@pytest.mark.parametrize("label", sorted(ARCHIVES))
def test_archives_are_opened_whatever_they_are_called_and_however_deep(tmp_path, label):
    name, data = ARCHIVES[label]
    z = make_zip(tmp_path / "rel.zip", {"Kairo-Setup-1/" + name: data})
    code, out = scan(tmp_path, "--zip", str(z), terms=[NAME, PLACE])
    assert "personal term #" in out and code == 1, out
    no_leak(out, NAME, PLACE)


def test_a_zip_with_a_damaged_directory_or_odd_names_is_read_from_its_local_headers(tmp_path):
    raw = bytearray(zbytes({"cafe.txt": TEXT}))
    raw[30 + 3] = 0xE9                                       # the name is now latin-1 ...
    raw[6] |= 0x08                                           # ... but flagged UTF-8 (local header)
    central = raw.index(b"PK\x01\x02")
    raw[central + 8 + 1] |= 0x08                             # (central directory entry)
    raw[central + 46 + 3] = 0xE9
    with pytest.raises(UnicodeDecodeError):
        zipfile.ZipFile(io.BytesIO(bytes(raw)))              # the standard library gives up on it
    z = make_zip(tmp_path / "rel.zip", {"Kairo-Setup-1/app/inner.zip": bytes(raw)})
    code, out = scan(tmp_path, "--zip", str(z), terms=[NAME, PLACE])
    assert code == 1 and "inner.zip!/caf" in out and "personal term #" in out, out


def test_archives_nested_too_deep_and_archives_that_cannot_be_opened_are_errors_in_a_zip_scan(tmp_path):
    cases = {
        "nested too deep": ("app/deep.zip", nested_zip(12, {"notes.txt": TEXT})),
        "zstd": ("app/pack.zst", b"\x28\xb5\x2f\xfd" + b"\x00" * 64),
        "7z": ("app/pack.7z", b"7z\xbc\xaf\x27\x1c" + b"\x00" * 64),
        "rar": ("app/pack.rar", b"Rar!\x1a\x07\x00" + b"\x00" * 64),
        "truncated gzip": ("app/cut.gz", gzip.compress(TEXT.encode() * 100)[:40]),
    }
    for label, (name, data) in cases.items():
        z = make_zip(tmp_path / f"rel-{label.replace(' ', '-')}.zip", {"Kairo-Setup-1/" + name: data})
        code, out = scan(tmp_path, "--zip", str(z), terms=[NAME, PLACE])
        assert code == 2 and "not scanned" in out, (label, out)
        # outside a release scan it is only a warning
        write(tmp_path / "t" / name, data)
    code, out = scan(tmp_path, "--tree", "t", terms=[NAME, PLACE])
    assert code == 0 and "warning" in out and "not scanned" in out, out
    code, out = scan(tmp_path, "--tree", "t", "--ci", terms=[NAME, PLACE])
    assert code == 2, out
    code, out = scan(tmp_path, "--tree", "t", "--strict", terms=[NAME, PLACE])
    assert code == 2, out


def _patch_zip(raw: bytes, local: dict[int, bytes], central: dict[int, bytes]) -> bytes:
    b = bytearray(raw)
    for off, val in local.items():
        b[off: off + len(val)] = val
    c = b.index(b"PK\x01\x02")
    for off, val in central.items():
        b[c + off: c + off + len(val)] = val
    return bytes(b)


def test_zip_members_that_cannot_be_read_are_errors_not_silence_and_never_a_crash(tmp_path):
    raw = zbytes({"app/notes.txt": TEXT * 20})
    cases = {
        "deflate64": _patch_zip(raw, {8: struct.pack("<H", 9)}, {10: struct.pack("<H", 9)}),
        "bad crc": _patch_zip(raw, {14: b"\x00\x00\x00\x00"}, {16: b"\x00\x00\x00\x00"}),
        "encrypted": _patch_zip(raw, {6: b"\x01"}, {8: b"\x01"}),
        "corrupt stream": _patch_zip(raw, {40: b"\xde\xad\xbe\xef\xde\xad\xbe\xef"}, {}),
    }
    for label, data in cases.items():
        p = write(tmp_path / f"{label.replace(' ', '-')}.zip", data)
        code, out = scan(tmp_path, "--zip", str(p), terms=[NAME, PLACE])
        assert code == 2 and "not scanned" in out and "Traceback" not in out, (label, out)
    # the same inside a release zip (a zip in a zip)
    z = make_zip(tmp_path / "rel.zip", {"Kairo-Setup-1/app/inner.zip": cases["corrupt stream"]})
    code, out = scan(tmp_path, "--zip", str(z), terms=[NAME, PLACE])
    assert code == 2 and "Traceback" not in out


def test_zip_comments_and_the_zip_files_own_name_are_checked(tmp_path):
    p = tmp_path / ("Kairo-Setup-" + NAME.replace(" ", "-") + ".zip")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("app/a.txt", "fine\n")
        z.comment = NAME.encode()
        z.getinfo("app/a.txt").comment = PLACE.encode()
    code, out = scan(tmp_path, "--zip", str(p), terms=[NAME, PLACE])
    assert code == 1
    assert "personal term #1 (list line 1) in the name" in out                       # the file name of the zip itself
    assert "(zip comment)" in out and "(entry comment)" in out
    no_leak(out, NAME, NAME.replace(" ", "-"), PLACE)
    write(tmp_path / ("bad-" + NAME.replace(" ", "-") + ".zip"), "not a zip")
    code, out = scan(tmp_path, "--zip", str(tmp_path / ("bad-" + NAME.replace(" ", "-") + ".zip")), terms=[NAME])
    assert code == 1 or code == 2
    no_leak(out, NAME, NAME.replace(" ", "-"), "testperson")


# ---------------------------------------------------------------- what cannot be scanned is not silently passed

def run_main(ps, capsys, monkeypatch, tmp_path, *argv, terms=None, env=None):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "nohome"))
    e = {"KAIRO_PERSONAL_TERMS": "\n".join(terms if terms is not None else [NAME, PLACE]), "KAIRO_DATA_DIR": str(tmp_path / "nodata")}
    e.update(env or {})
    code = ps.main(list(argv), env=e)
    cap = capsys.readouterr()
    return code, cap.out + cap.err


def test_a_file_over_the_size_limit_is_an_error_in_ci_and_in_a_zip_scan_and_a_warning_otherwise(ps, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ps, "MAX_BYTES", 4000)
    write(tmp_path / "t" / "big.log", NAME + "\n" + "x" * 5000)
    code, out = run_main(ps, capsys, monkeypatch, tmp_path, "--tree", str(tmp_path / "t"))
    assert code == 0 and "warning" in out and "not scanned" in out
    code, out = run_main(ps, capsys, monkeypatch, tmp_path, "--tree", str(tmp_path / "t"), "--ci")
    assert code == 2 and "error" in out and "not scanned" in out
    z = make_zip(tmp_path / "rel.zip", {"Kairo-Setup-1/app/big.js": NAME + "\n" + "x" * 5000})
    code, out = run_main(ps, capsys, monkeypatch, tmp_path, "--zip", str(z))
    assert code == 2 and "not scanned" in out


def test_a_big_text_is_scanned_in_pieces_with_the_right_line_numbers(ps, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ps, "CHUNK_BYTES", 3000)
    monkeypatch.setattr(ps, "CHUNK_OVERLAP", 200)
    lines = ["line %04d filler filler filler filler" % n for n in range(400)]            # about 15 KB
    lines[120] = "here: " + NAME
    lines[250] = NAME.split()[0] + "\n" + NAME.split()[1]                                 # the name is split over two lines
    lines[399] = "last line " + PLACE
    write(tmp_path / "t" / "export.json", "\n".join(lines) + "\n")
    code, out = run_main(ps, capsys, monkeypatch, tmp_path, "--tree", str(tmp_path / "t"), "--ci")
    assert code == 1, out
    assert "export.json:121: personal term #1" in out
    assert "export.json:251: personal term #1" in out or "export.json:252: personal term #1" in out
    assert "export.json:401: personal term #2" in out or "export.json:400: personal term #2" in out
    assert "not scanned" not in out
    no_leak(out, NAME, PLACE)


def test_a_big_file_with_a_term_in_the_first_line_is_reported_not_skipped(ps, tmp_path, capsys, monkeypatch):
    """The old limit was 32 MB and a bigger file only got a warning. Now it is read, in pieces."""
    monkeypatch.setattr(ps, "CHUNK_BYTES", 100_000)
    write(tmp_path / "t" / "big.log", NAME + "\n" + ("x" * 99 + "\n") * 3000)
    code, out = run_main(ps, capsys, monkeypatch, tmp_path, "--tree", str(tmp_path / "t"), "--ci")
    assert code == 1 and "big.log:1: personal term #1" in out


def test_the_terms_check_can_be_left_out_on_purpose_and_says_so(tmp_path):
    write(tmp_path / "t" / "a.txt", f"{NAME}\n{GH_PAT}\n")
    code, out = scan(tmp_path, "--tree", "t", "--secrets-only", "--ci", GITHUB_REF="refs/heads/main")
    assert code == 1 and "a.txt:2: secret: GitHub token" in out and "a.txt:1" not in out
    assert "--secrets-only" in out and "PERSONAL_TERMS" not in out


# ---------------------------------------------------------------- the terms list

def test_a_list_on_one_line_is_an_error_not_a_single_useless_rule(tmp_path):
    write(tmp_path / "t" / "a.txt", f"{NAME} was in {PLACE}\n")
    for bad in (f"{NAME}, {PLACE}", f"{NAME}; {PLACE}"):
        code, out = scan(tmp_path, "--ci", "--tree", "t", GITHUB_REF="refs/heads/main", KAIRO_PERSONAL_TERMS=bad)
        assert code == 2 and "personal term #1" in out and "comma or semicolon" in out and "clean" not in out
        no_leak(out, NAME, PLACE)


def test_a_literal_backslash_n_in_the_secret_means_a_line_break(tmp_path):
    """gh secret set --body 'a\\nb' stores the two characters."""
    write(tmp_path / "t" / "a.txt", f"{NAME} was in {PLACE}\n")
    code, out = scan(tmp_path, "--ci", "--tree", "t", GITHUB_REF="refs/heads/main",
                     KAIRO_PERSONAL_TERMS=NAME + "\\n" + PLACE)
    assert code == 1 and "2 rules" in out and "a.txt:1: personal term #1" in out and "a.txt:1: personal term #2" in out
    assert "literal" in out                                                    # and the owner is told to fix the secret
    code, out = scan(tmp_path, "--ci", "--tree", "t", KAIRO_PERSONAL_TERMS=NAME + "\\r\\n" + PLACE)
    assert code == 1 and "2 rules" in out


def test_a_list_saved_by_windows_powershell_is_read(tmp_path):
    write(tmp_path / "t" / "a.txt", f"{UML} and {NAME}\n")
    lists = {
        "utf-8": (UML + "\r\n" + NAME + "\r\n").encode("utf-8"),
        "utf-8-bom": b"\xef\xbb\xbf" + (UML + "\r\n" + NAME + "\r\n").encode("utf-8"),
        "windows-1252 (Set-Content)": (UML + "\r\n" + NAME + "\r\n").encode("cp1252"),
        "utf-16-le-bom (Out-File, >)": (UML + "\r\n" + NAME + "\r\n").encode("utf-16"),
        "utf-16-be-bom": b"\xfe\xff" + (UML + "\r\n" + NAME + "\r\n").encode("utf-16-be"),
        "utf-16-le-no-bom": (UML + "\r\n" + NAME + "\r\n").encode("utf-16-le"),
        "utf-16-one-line-no-newline": (UML + "\r\n" + NAME).encode("utf-16"),
    }
    for label, data in lists.items():
        f = write(tmp_path / "terms.txt", data)
        code, out = scan(tmp_path, "--tree", "t", "--terms-file", str(f))
        assert code == 1 and "a.txt:1: personal term #1" in out and "a.txt:1: personal term #2" in out, (label, out)
        assert "2 rules" in out, (label, out)
        no_leak(out, NAME, UML)
    f = write(tmp_path / "terms.txt", (UML + "\r\n").encode("cp1252"))
    code, out = scan(tmp_path, "--tree", "t", "--terms-file", str(f))
    assert "Windows-1252" in out and "UTF-8" in out                                  # and the owner is told to save it as UTF-8


def test_a_list_with_broken_text_is_an_error_not_a_rule_that_matches_nothing(tmp_path):
    write(tmp_path / "t" / "a.txt", "x\n")
    f = write(tmp_path / "terms.txt", b"first\r\n\x81\x8d\x90 broken\r\n")           # bytes that are nothing in cp1252
    code, out = scan(tmp_path, "--tree", "t", "--terms-file", str(f))
    assert code == 2 and "personal term #2" in out and "UTF-8" in out


def test_error_messages_never_show_a_term(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, tmp_path, "base", {"a.txt": "x\n"})
    bogus = "1234567890" * 4
    write(repo / ".git" / "refs" / "heads" / f"fix-{PLACE.lower()}-notes", bogus + "\n")          # a ref to a missing object
    code, out = scan(tmp_path, "--history", "--ci", terms=[PLACE], cwd=repo, GITHUB_ACTIONS="true")
    assert code == 2 and "error" in out, out
    no_leak(out, PLACE)
    code, out = scan(tmp_path, "--history", "--repo", str(tmp_path / "nowhere" / PLACE), "--ci", terms=[PLACE],
                     GITHUB_ACTIONS="true")
    assert code == 2
    no_leak(out, PLACE)


def test_redacted_names_are_numbered_not_hashed(tmp_path):
    import hashlib
    name = f"docs/{PLACE}/{NAME.replace(' ', '-')}.md"
    write(tmp_path / "t" / name, "x\n")
    code, out = scan(tmp_path, "--tree", "t", terms=[NAME, PLACE])
    assert code == 1 and "<redacted name #1>" in out
    for digest in (hashlib.sha256(name.encode()).hexdigest()[:8], hashlib.sha1(name.encode()).hexdigest()[:8],
                   hashlib.md5(name.encode()).hexdigest()[:8]):
        assert digest not in out
    no_leak(out, NAME, PLACE, "testperson")


# ---------------------------------------------------------------- secrets

def _key(name_start: str, name_end: str, value: str, sep: str = "=") -> str:
    """name_start + name_end is the variable; split so that this file does not hold a whole assignment."""
    return j(name_start, name_end, sep, value)


RND43 = "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF3hJ6kL9nP2Q"                      # 43 characters like a base64url P-256 key
RND32 = "q7Xk2Lm9Vb4Zr8Tn3Wp5Hs6Jd1Fg0Ac4"
B2 = j("K00", "5aB3dE6gH9jK2mN5pQ8sT1vW4yZ7b")


@pytest.mark.parametrize("text", [
    "ENCRYPTION_K" + "EY = '" + RND43 + "'",
    "SIGNING_K" + "EY=" + j("3f9a", "c27d41b65e08a9d3c1f7b2e45a6d8c90b3e1f4a72d5c8b60e9a1f3d7c2b4e58"),
    "signingK" + "ey: " + RND43,
    "B2_ACCOUNT_K" + "EY=" + B2,
    "SHARED_SECR" + "ET=" + RND43,
    "client_k" + "ey = \"" + RND32 + "Xy\"",
    "ap" + "p_secret_k" + "ey: " + RND43,
    "VAPID_PRIV" + "ATE=" + RND43,
    "VAPID_PRIV" + "ATE_KEY=" + RND43,
    '{"vapid": {"priv' + 'ate": "' + RND43 + '"}}',
    json.dumps({"kty": "EC", "crv": "P-256", "x": RND43, "y": RND43[::-1], "d": RND43.swapcase()}),
    "{'kty': 'EC', 'd': '" + RND43 + "'}",
    "https://kairo:" + j("m1m8KJDovRzW", "2hwrrD94Mvmh") + "@backup.invalid/repo",
    "sess" + "ion=" + RND43,
    "Set-Cookie: sess" + "ion_id=" + RND43 + "; HttpOnly",
    "HEALTHCHECK_URL=https://hc-pi" + "ng.com/ef11de8b-fbe8-43e6-bfa4-8bdf25079b98",
], ids=["encryption-key", "signing-key-hex", "camel-signing-key", "b2-account-key", "shared-secret", "client-key", "app-secret-key",
        "vapid-private", "vapid-private-key", "vapid-nested-json", "jwk-json", "jwk-python-dict", "password-in-url", "session-cookie",
        "set-cookie", "healthchecks-url"])
def test_more_shapes_of_secrets_are_found(ps, text):
    found = ps.scan_secrets("first\n" + text + "\nlast\n")
    assert found and found[0][0] == 2, (text, found)


@pytest.mark.parametrize("text", [
    "sha_k" + "ey = '" + RND43 + "'", "public_k" + "ey = '" + RND43 + "'", "PUBLIC_K" + "EY=" + RND43,
    "CACHE_K" + "EY = 'loops.cleanup.2026-10-19'", "SEEN_K" + "EY = 'loops.sent_seen'", "idempotency_k" + "ey = 'create-task-0001-abcdef'",
    "fingerprint_k" + "ey = '" + RND43 + "'", "hashed_secr" + "et = '" + RND43 + "'", "KEY = '" + RND43 + "'",
    "k" + "ey: value", "monkey_patch = 'abcdefghijklmnopqrstuvwx0123'",
    json.dumps({"kty": "EC", "crv": "P-256", "x": RND43}),                       # a public JWK has no d
    json.dumps({"d": RND43}),                                                      # a d without kty is not a key
    "sess" + "ion = requests.Session()", "sess" + "ion_id = get_session_id_for_the_current_request()", "cookie_jar = CookieJar()",
    "https://user:pass@example.org/", "https://example.org:8080/path", "hc-pi" + "ng.com/00000000-0000-0000-0000-000000000000",
    "DB_K" + 'EY = "kairo:calendar:last_sync_token_2026"', "STATE_K" + 'EY = "Kairo.Calendar.LastSync2026.v2"',
], ids=["sha-key", "public-key", "public-key-upper", "dotted-cache-key", "seen-key", "idempotency-key", "fingerprint-key",
        "hashed-secret", "bare-key", "key-word", "monkey", "public-jwk", "d-without-kty", "session-object", "session-id-call",
        "cookie-jar", "short-url-password", "port-in-url", "zero-uuid", "colon-identifier", "mixed-case-identifier"])
def test_names_that_say_it_is_not_a_secret_are_left_alone(ps, text):
    assert ps.scan_secrets(text + "\n") == [], text


def test_a_shared_secret_is_not_taken_for_a_hash(ps):
    """The old rule skipped any name that contained 'sha', so SHAred_secret passed."""
    assert ps.scan_secrets("SHARED_SECR" + "ET = '" + RND43 + "'\n")
    assert ps.scan_secrets("shared_secr" + "et = '" + RND43 + "'\n")
    assert not ps.scan_secrets("secret_sha256 = '" + RND43 + "'\n")


def test_forbidden_paths_new_shapes(ps):
    for path in ("server/vault/10 Profile/ABOUT_ME.md", "secrets/google_token.json", "data/secrets/logins/x.bin", "db/kairo.db",
                 "db/kairo.db3", "backups/kairo.sqlite.bak", "prod.env", "config/kairo.env", ".envrc", ".env-prod", ".env ",
                 ".env.", ".private/notes.md", "capture-token.txt", "x/capture-token", "Secrets/x", "x/Vault/y.md",
                 "a/b/c.PDF", "photos/me.JPEG", "fernet.key", "keys/server.KEY"):
        assert ps.forbidden_path(path), path
    for path in ("server/app/vault.py", "server/app/secretbox.py", "docs/vaults.md", "server/tests/test_vault.py",
                 ".env.example", "environment.yaml", "server/app/secrets_check.py", "web/public/x.pdf", "x/web/dist/y.pdf"):
        assert ps.forbidden_path(path) == [], path


# ---------------------------------------------------------------- git: staged, history, messages

def test_staged_reads_the_staged_blob_by_id_so_a_colon_in_a_name_changes_nothing(tmp_path):
    if sys.platform == "win32":
        pytest.skip("a colon cannot be in a file name on Windows")
    repo = make_repo(tmp_path)
    commit(repo, tmp_path, "start", {"notes.txt": "clean\n"})
    write(repo / "0:notes.txt", f"{NAME}, {PLACE}\n")
    write(repo / "1:other.txt", f"{NAME}\n")
    write(repo / "2:x.txt", f"{PLACE}\n")
    stage(repo, tmp_path, "0:notes.txt", "1:other.txt", "2:x.txt")
    code, out = scan(tmp_path, "--staged", terms=[NAME, PLACE], cwd=repo)
    assert code == 1
    assert "0:notes.txt:1: personal term #1" in out and "1:other.txt:1: personal term #1" in out
    assert "2:x.txt:1: personal term #2" in out
    assert "no file content" not in out
    no_leak(out, NAME, PLACE)


def test_staged_is_not_fooled_by_a_diff_driver_that_hides_lines(tmp_path):
    repo = make_repo(tmp_path)
    write(repo / ".gitattributes", "*.txt diff=hide\n")
    commit(repo, tmp_path, "attrs")
    git(repo, tmp_path, "config", "diff.hide.textconv", "sh -c 'true' #")
    write(repo / "a.txt", f"{NAME}\n")
    stage(repo, tmp_path, "a.txt")
    code, out = scan(tmp_path, "--staged", terms=[NAME], cwd=repo)
    assert code == 1 and "a.txt:1: personal term #1" in out, out


def test_staged_catches_a_match_that_starts_on_an_old_line_and_ends_on_a_new_one(tmp_path):
    repo = make_repo(tmp_path)
    first, last = NAME.split()
    commit(repo, tmp_path, "one", {"a.txt": f"name: {first}\n"})
    write(repo / "a.txt", f"name: {first}\n{last} from the school\n")
    stage(repo, tmp_path, "a.txt")
    code, out = scan(tmp_path, "--staged", terms=[NAME], cwd=repo)
    assert code == 1 and "a.txt:1: personal term #1" in out, out
    # a key block whose header was committed earlier and whose body is pasted in now
    commit(repo, tmp_path, "template", {"k.pem.txt": "-----BEGIN " + "PRIVATE KEY-----\nPASTE THE KEY HERE\n-----END " + "PRIVATE KEY-----\n"})
    code, out = scan(tmp_path, "--staged", terms=[NAME], cwd=repo)
    git(repo, tmp_path, "reset", "-q")
    write(repo / "k.pem.txt", "-----BEGIN " + "PRIVATE KEY-----\n" + PEM_RSA.split("\n")[1] + "\n-----END " + "PRIVATE KEY-----\n")
    stage(repo, tmp_path, "k.pem.txt")
    code, out = scan(tmp_path, "--staged", terms=[NAME], cwd=repo)
    assert code == 1 and "k.pem.txt:1: secret: private key" in out, out
    no_leak(out, "MIIBOgIBAAJBAKj34")


def test_staged_still_ignores_a_hit_that_is_entirely_on_old_lines(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, tmp_path, "old", {"a.txt": f"first\n{NAME}\nlast\n"})
    write(repo / "a.txt", f"first\n{NAME}\nlast\nadded\n")
    stage(repo, tmp_path, "a.txt")
    code, out = scan(tmp_path, "--staged", terms=[NAME], cwd=repo)
    assert code == 0, out


def test_staged_names_binaries_archives_documents_and_symlinks(tmp_path):
    repo = make_repo(tmp_path)
    write(repo / "pic.png", png_with_text(b"Comment", NAME.encode()))
    write(repo / "seed.tar.gz", tar_bytes({"x/seed.md": TEXT.encode()}, "w:gz"))
    write(repo / "letter.pdf", pdf_with(NAME))
    write(repo / f"{NAME.replace(' ', '')}.md", "hello\n")
    write(repo / "kairo", b"SQLite format 3\x00" + b"\x00" * 50)
    stage(repo, tmp_path, "pic.png", "seed.tar.gz", "letter.pdf", f"{NAME.replace(' ', '')}.md", "kairo")
    code, out = scan(tmp_path, "--staged", terms=[NAME, PLACE], cwd=repo)
    assert code == 1
    assert "pic.png: personal term #1" in out and "seed.tar.gz!/seed.tar!/x/seed.md:2: personal term #1" in out
    assert "letter.pdf: forbidden path: document or photo" in out
    assert "personal term #1 (list line 1) in the name" in out
    assert "kairo: forbidden content: SQLite" in out
    no_leak(out, NAME, PLACE, NAME.replace(" ", ""))


def test_history_finds_files_added_by_a_merge_even_when_their_content_is_not_new(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, tmp_path, "base", {"a.txt": "hello\n", "pkg/__init__.py": ""})
    git(repo, tmp_path, "switch", "-q", "-c", "side")
    commit(repo, tmp_path, "side", {"s.txt": "side\n"})
    git(repo, tmp_path, "switch", "-q", "main")
    commit(repo, tmp_path, "main work", {"m.txt": "main\n"})
    git(repo, tmp_path, "merge", "-q", "--no-commit", "--no-ff", "side")
    write(repo / (NAME.replace(" ", "_").lower() + ".txt"), "hello\n")                    # the blob of a.txt
    write(repo / (PLACE.lower() + "_notes.txt"), "")                                      # the empty blob of __init__.py
    write(repo / "private" / "a.txt", "hello\n")
    git(repo, tmp_path, "add", "-A", "-f")
    git(repo, tmp_path, "commit", "-q", "-m", "merge")
    code, out = scan(tmp_path, "--history", terms=[NAME, PLACE], cwd=repo)
    assert code == 1, out
    assert out.count("personal term #1 (list line 1) in the name") == 1
    assert "personal term #2 (list line 2) in the name" in out
    assert "private/a.txt: forbidden path: private/ folder" in out or "private/: forbidden path: private/ folder" in out
    no_leak(out, NAME, PLACE, NAME.replace(" ", "_"))


def _commit_object(repo: Path, tmp_path: Path, extra_header: str, message: str = "clean message\n") -> str:
    tree = git(repo, tmp_path, "rev-parse", "HEAD^{tree}").strip()
    parent = git(repo, tmp_path, "rev-parse", "HEAD").strip()
    body = (f"tree {tree}\nparent {parent}\nauthor Dev <dev@example.com> 1700000000 +0000\n"
            f"committer Dev <dev@example.com> 1700000000 +0000\n{extra_header}\n{message}")
    p = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"], cwd=repo, input=body.encode(), capture_output=True,
                       env=clean_env(tmp_path))
    assert p.returncode == 0, p.stderr
    sha = p.stdout.decode().strip()
    git(repo, tmp_path, "update-ref", "refs/heads/main", sha)
    return sha


def test_history_reads_the_whole_commit_object_including_extra_headers(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, tmp_path, "base", {"a.txt": "x\n"})
    _commit_object(repo, tmp_path, f"x-origin {NAME}\n")
    code, out = scan(tmp_path, "--history", terms=[NAME], cwd=repo)
    assert code == 1 and "header x-origin: personal term #1" in out, out
    no_leak(out, NAME)


@pytest.mark.parametrize("header", ["encoding {u}", "{u} clean", "{d}", "{u} {name}"])
def test_history_reads_every_part_of_a_header_and_never_prints_its_key(tmp_path, header):
    """The value of `encoding` was skipped, a header's key was never scanned, and an unknown key went into the report
    as written."""
    under, dashed = NAME.replace(" ", "_"), NAME.replace(" ", "-")
    repo = make_repo(tmp_path)
    commit(repo, tmp_path, "base", {"a.txt": "x\n"})
    _commit_object(repo, tmp_path, header.format(u=under, d=dashed, name=PLACE) + "\n")
    code, out = scan(tmp_path, "--history", terms=[NAME, PLACE], cwd=repo)
    assert code == 1 and "personal term #1" in out, out
    no_leak(out, NAME, under, dashed, PLACE)


def test_history_sees_the_tag_that_a_signed_merge_carries_in_its_mergetag_header(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, tmp_path, "base", {"a.txt": "x\n"})
    head = git(repo, tmp_path, "rev-parse", "HEAD").strip()
    mergetag = (f"mergetag object {head}\n type commit\n tag v1\n tagger Dev <dev@example.com> 1700000000 +0000\n \n"
                f" released by {NAME}\n")
    _commit_object(repo, tmp_path, mergetag)
    code, out = scan(tmp_path, "--history", terms=[NAME], cwd=repo)
    assert code == 1 and "header mergetag: personal term #1" in out, out
    no_leak(out, NAME)


def test_history_survives_control_characters_in_a_message_and_cannot_be_fooled_by_them(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, tmp_path, "base", {"a.txt": "x\n"})
    write(tmp_path / "m1.txt", b"subject\x1ejunk\n")
    git(repo, tmp_path, "commit", "-q", "--allow-empty", "-F", str(tmp_path / "m1.txt"), "--cleanup=verbatim")
    write(tmp_path / "m2.txt", b"Fix\x1e" + f"{NAME} lives in {PLACE}".encode() + b"\x1fa\x1fb\x1fc\x1fd\x1fok\n")
    git(repo, tmp_path, "commit", "-q", "--allow-empty", "-F", str(tmp_path / "m2.txt"), "--cleanup=verbatim")
    code, out = scan(tmp_path, "--history", terms=[NAME, PLACE], cwd=repo)
    assert code == 1 and "Traceback" not in out and "message:1: personal term #1" in out, out
    no_leak(out, NAME, PLACE)


def test_history_reads_archives_in_old_commits(tmp_path):
    repo = make_repo(tmp_path)
    seed = tar_bytes({"private/profile/ABOUT_ME.md": TEXT.encode(), "deploy.ps1": (j('$t = "', GH_PAT, '"\r\n')).encode()}, "w:gz")
    commit(repo, tmp_path, "oops", {"backup/seed.tar.gz": seed, "backup/client.json.gz": gzip.compress(json.dumps({"s": GOCSPX}).encode())})
    commit(repo, tmp_path, "cleanup", remove=["backup/seed.tar.gz", "backup/client.json.gz"])
    code, out = scan(tmp_path, "--history", terms=[NAME, PLACE], cwd=repo)
    assert code == 1
    assert "private/ folder" in out and "secret: GitHub token" in out and "secret: Google OAuth client secret" in out
    assert "personal term #1" in out
    no_leak(out, NAME, PLACE, GH_PAT, GOCSPX)


def test_tree_checks_the_names_of_symlinks_and_what_they_point_to(tmp_path):
    if sys.platform == "win32":
        pytest.skip("symlinks need rights on Windows")
    t = tmp_path / "t"
    (t / "private").mkdir(parents=True)
    os.symlink("/etc/hostname", t / (NAME.replace(" ", "_") + ".txt"))
    os.symlink("/etc/hostname", t / "private" / "profile.md")
    os.symlink("../x", t / ".env")
    os.symlink(f"/home/{PLACE.lower()}/notes", t / "link")
    code, out = scan(tmp_path, "--tree", "t", terms=[NAME, PLACE])
    assert code == 1
    assert "personal term #1 (list line 1) in the name" in out
    assert "private/profile.md: forbidden path: private/ folder" in out
    assert ".env: forbidden path" in out
    assert "link: personal term #2" in out
    no_leak(out, NAME, PLACE)


def test_a_gitleaks_report_is_printed_without_the_matched_text(tmp_path):
    report = [{"RuleID": "generic-api-key", "Description": "x", "StartLine": 7, "EndLine": 7,
               "Match": f"token_for_{PLACE.lower()} = SECRETVALUE1  # {NAME}", "Secret": "SECRETVALUE1", "File": f"{PLACE}/{NAME}.py",
               "Commit": "0123456789abcdef0123456789abcdef01234567", "Author": NAME, "Email": f"{PLACE.lower()}@example.org",
               "Message": f"{NAME} added this", "Fingerprint": "x"},
              {"RuleID": "aws-access-token", "StartLine": 2, "File": "server/app/x.py", "Commit": "abcdef0123456789",
               "Author": NAME, "Match": "AKIA...", "Secret": "SECRETVALUE2"}]
    f = write(tmp_path / "gl.json", json.dumps(report))
    code, out = scan(tmp_path, "--gitleaks-report", str(f), terms=[NAME, PLACE])
    assert code == 1
    assert "<redacted name #1>:7: gitleaks: generic-api-key [commit 0123456]" in out
    assert "server/app/x.py:2: gitleaks: aws-access-token [commit abcdef0]" in out
    no_leak(out, NAME, PLACE, "SECRETVALUE", "AKIA")
    write(tmp_path / "gl.json", "[]")
    code, out = scan(tmp_path, "--gitleaks-report", str(tmp_path / "gl.json"), terms=[NAME])
    assert code == 0


def test_a_commit_message_in_latin_1_is_read(tmp_path):
    f = write(tmp_path / "m.txt", b"subject\n\n" + UML.encode("latin-1") + b" 5\n")
    code, out = scan(tmp_path, "--message-file", str(f), terms=[UML])
    assert code == 1 and "commit message:3: personal term #1" in out
    no_leak(out, UML)


def test_history_is_not_hidden_by_a_replace_ref(tmp_path):
    """refs/replace/ shows another object in place of one; what gets pushed is the real one."""
    repo = make_repo(tmp_path)
    commit(repo, tmp_path, "first", {"a.txt": f"{NAME}\n"})
    old = git(repo, tmp_path, "rev-parse", "HEAD").strip()
    commit(repo, tmp_path, "second", {"a.txt": "clean\n"})
    git(repo, tmp_path, "replace", old, "HEAD")
    assert NAME not in git(repo, tmp_path, "show", f"{old}:a.txt")                 # (git shows the replacement)
    code, out = scan(tmp_path, "--history", terms=[NAME], cwd=repo)
    assert code == 1 and "a.txt:1: personal term #1" in out, out


def test_history_finds_a_name_run_together_in_author_and_committer_emails(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, tmp_path, "one", {"a.txt": "1\n"}, GIT_AUTHOR_EMAIL=NAME.replace(" ", "").lower() + "@example.org")
    commit(repo, tmp_path, "two", {"b.txt": "2\n"},
           GIT_COMMITTER_EMAIL="".join(w.capitalize() for w in NAME.split()) + "@users.noreply.example.org")
    code, out = scan(tmp_path, "--history", terms=[NAME], cwd=repo)
    assert code == 1 and "author email: personal term #1" in out and "committer email: personal term #1" in out, out
    no_leak(out, NAME, NAME.replace(" ", ""))


def test_a_zip_member_named_after_a_person_without_a_separator_is_found(tmp_path):
    z = make_zip(tmp_path / "rel.zip", {"Kairo-Setup-1/app/" + NAME.replace(" ", "") + ".txt": "hello\n"})
    code, out = scan(tmp_path, "--zip", str(z), terms=[NAME])
    assert code == 1 and "in the name" in out
    no_leak(out, NAME, NAME.replace(" ", ""))


def test_a_zip_member_name_written_by_an_old_windows_tool_is_read_the_way_it_was_written(tmp_path):
    """No UTF-8 flag: zipfile reads the name as DOS (cp437), where the byte of the u-umlaut is a different letter."""
    plain = UML.replace(U, "x")
    raw = zbytes({plain + ".txt": "hello\n"}).replace(plain.encode(), UML.encode("latin-1"))
    assert raw.count(UML.encode("latin-1")) == 2                                       # local header and central directory
    z = write(tmp_path / "rel.zip", raw)
    code, out = scan(tmp_path, "--zip", str(z), terms=[UML])
    assert code == 1 and "personal term #1 (list line 1) in the name" in out, out
    no_leak(out, UML, UML.replace(U, "ue"))


# ---------------------------------------------------------------- what tells gitleaks to look away

GL_ALLOW = j("gitleaks", ":allow")                            # gitleaks skips any line that holds this


def test_a_gitleaks_ignore_file_is_never_committed_or_shipped(ps):
    """gitleaks always reads .gitleaksignore from the folder it scans, whatever config CI gives it: a pull request
    could add one with the fingerprint of its own finding."""
    for path in (".gitleaksignore", "server/.gitleaksignore", "Kairo-Setup-1/app/.GITLEAKSIGNORE"):
        assert any("gitleaks" in r for r in ps.forbidden_path(path)), path


def test_a_gitleaks_allow_comment_is_a_finding_outside_the_folders_for_fakes(tmp_path):
    line = f"x = '{GH_PAT[:10]}'  # {GL_ALLOW}\n"
    code, out = hit(tmp_path, f"ok\n{line}", name="server/app/x.py")
    assert code == 1 and "server/app/x.py:2: secret: gitleaks allow comment" in out, out
    code, out = hit(tmp_path, f"ok\n{line}", name="server/tests/test_x.py")
    assert code == 0, out                                                         # deliberate fakes in tests may carry it
    repo = make_repo(tmp_path)
    commit(repo, tmp_path, "base", {"a.txt": "x\n"})
    commit(repo, tmp_path, "quiet please", {"cfg.toml": f"key = 1  # {GL_ALLOW}\n", ".gitleaksignore": "abc:cfg.toml:generic:1\n"})
    code, out = scan(tmp_path, "--history", "HEAD~1..HEAD", terms=[NAME], cwd=repo)
    assert code == 1 and "cfg.toml:1: secret: gitleaks allow comment" in out and ".gitleaksignore: forbidden path" in out, out
