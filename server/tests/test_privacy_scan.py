"""scripts/privacy_scan.py: personal data and secrets can never get into the repo or a release again
(owner's master prompt 0.6: the code may become public, all personal data lives on the PC only).

Every name, place and key here is made up. They are built from pieces (j) so that this file does not
contain any of them, because one test runs the scanner over the whole repo with these terms and it must
come out clean."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import zipfile
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


def no_leak(out: str, *secrets: str) -> None:
    low = out.lower()
    for s in secrets:
        assert s.lower() not in low, "the scanner printed something it must never print"


# ---------------------------------------------------------------- matching rules (unit)

def test_fold_umlauts_case_and_accents(ps):
    assert ps.fold("Müller") == "mueller"
    assert ps.fold("ÖL Äpfel Üben") == "oel aepfel ueben"
    assert ps.fold("Straße") == ps.fold("STRASSE") == "strasse"
    assert ps.fold("Mu\u0308ller") == "mueller"          # u + combining diaeresis (macOS file names)
    assert ps.fold("José") == "jose"


@pytest.mark.parametrize("term,text", [
    (UML, UML), (UML, UML.lower()), (UML, UML.upper()), (UML, UML_ASCII), (UML, UML_ASCII.upper()),
    (UML_ASCII, UML),
    (STRASSE, STRASSE_ASCII), (STRASSE, STRASSE_ASCII.upper()), (STRASSE_ASCII, STRASSE), (STRASSE, "Ma\u00dftal"),
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


def test_a_missing_terms_file_falls_back_to_the_env_list_with_a_warning(tmp_path):
    write(tmp_path / "t" / "a.txt", f"about {PLACE}\n")
    code, out = scan(tmp_path, "--tree", "t", terms=[PLACE], KAIRO_PERSONAL_TERMS_FILE=str(tmp_path / "nope.txt"))
    assert code == 1 and "warning" in out.lower()


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


def test_tree_scans_binary_files_that_hold_text_but_not_pictures(tmp_path):
    write(tmp_path / "t" / "store.dat", b"\x00\x01\x02 " + NAME.encode() + b" \x00\xff\x00")
    write(tmp_path / "t" / "pic.png", b"\x89PNG\r\n\x1a\n\x00" + NAME.encode() + b"\x00")
    code, out = scan(tmp_path, "--tree", "t", terms=[NAME])
    assert code == 1 and "store.dat" in out and "pic.png" not in out


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


def test_files_with_the_allow_pragma_pass_the_secret_check(tmp_path):
    write(tmp_path / "t" / "cfg.txt", f"key = {GH_PAT}  # privacy-scan: allow\n")
    code, out = scan(tmp_path, "--tree", "t", terms=[NAME])
    assert code == 0, out


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


def test_the_repos_own_fake_test_secrets_are_not_flagged(ps):
    offenders = []
    for p in (REPO_ROOT / "server" / "tests").rglob("*.py"):
        for line, rule in ps.scan_secrets(p.read_text(encoding="utf-8")):
            offenders.append(f"{p.name}:{line}: {rule}")
    assert offenders == []
