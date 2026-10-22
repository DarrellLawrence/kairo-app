#!/usr/bin/env python3
"""Keep personal data and secrets out of the repo and out of a release (owner's master prompt 0.6).

    python scripts/privacy_scan.py --staged                  # what a commit adds (the pre-commit hook)
    python scripts/privacy_scan.py --tree [DIR]              # a directory (default: the current one)
    python scripts/privacy_scan.py --history [REF ...]       # every commit reachable from the refs (default: all refs):
                                                             #   file contents, commit messages, author/committer, ref names
    python scripts/privacy_scan.py --zip dist/Kairo-Setup-*.zip
    python scripts/privacy_scan.py --message-file FILE       # a commit message (the commit-msg hook)
    add --ci in GitHub Actions (annotations; a missing terms list on main is an error)

Two detectors, plus a list of paths that must never be committed:

1. The owner's personal TERMS (names, places, numbers ...), read from a file OUTSIDE the repo. The first of
   these that exists wins:
     --terms-file FILE
     env KAIRO_PERSONAL_TERMS_FILE
     <data dir>/config/personal-terms.txt  (data dir = env KAIRO_DATA_DIR; on Windows C:\\Kairo\\data)
     ~/.config/kairo/personal-terms.txt
     env KAIRO_PERSONAL_TERMS             (the same list as text, for CI from a GitHub secret)
   One rule per line. Lines starting with # and empty lines are ignored. A line is a term (words, any case,
   umlauts folded: ue = u-umlaut, ss = sharp s; any punctuation between the words; never inside a longer word)
   or "regex:<pattern>" (case-insensitive; it sees the text as written and with umlauts folded).
   A matched term is NEVER printed, not in a log, not in CI: only file:line and the rule number are.
   Without a list the check is skipped with a warning (exit 0), but with --ci on refs/heads/main it is an error
   (exit 2), so the missing secret is noticed.
2. Secrets: private key blocks, Google/GitHub/Tailscale/OpenAI/AWS/Slack/Stripe keys, JSON web tokens and
   password/secret/token assignments with long random values. Known fake values and placeholders are allowed;
   a line can carry "privacy-scan: allow" for a deliberate fake. Only the rule's name is printed.
3. Paths: private/, data/, devdata/ folders, *.local.* files, .env (not .env.example), *.sqlite, the terms list
   itself, key stores, Google client secrets.

Exit codes: 0 clean, 1 findings, 2 could not do the check (bad list, no list on main in CI, bad zip, shallow clone in CI).

Standard library only (Python 3.12, the project's version); Windows and Linux.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import itertools
import math
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

TERMS_FILE_NAME = "personal-terms.txt"
WINDOWS_DATA_DIR = "C:\\Kairo\\data"

MAX_BYTES = 32 * 1024 * 1024            # a bigger file is reported as skipped, never silently
MAX_ZIP_DEPTH = 3                       # zips inside zips (a .docx is one)
MAX_ZIP_MEMBERS = 50_000
MAX_ZIP_TOTAL = 2 * 1024 ** 3           # uncompressed bytes read from archives in one run
MAX_FINDINGS_PER_FILE = 25
MAX_HITS_PER_RULE = 200
MAX_LINES_PRINTED = 300
MAX_ANNOTATIONS = 10                    # GitHub shows 10 per step

# Binary files that cannot hold readable text: only their names are checked.
SKIP_BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tif", ".tiff", ".heic", ".woff", ".woff2", ".ttf",
    ".otf", ".eot", ".mp3", ".mp4", ".m4a", ".wav", ".ogg", ".flac", ".webm", ".mov", ".avi", ".onnx", ".gguf",
    ".bin", ".pt", ".safetensors", ".npy", ".pyc", ".pyd", ".so", ".dll", ".exe", ".gz", ".tgz", ".bz2", ".xz",
    ".7z", ".rar", ".zst", ".pdf",
}


# ---------------------------------------------------------------- folding

_PUNCT = str.maketrans({"\u2019": "'", "\u2018": "'", "\u00b4": "'", "\u2013": "-", "\u2014": "-", "\u2010": "-",
                        "\u2011": "-"})


def fold(text: str) -> str:
    """Lower case, umlauts as their two-letter form (ue, oe, ae, ss), other accents dropped, quotes and dashes plain.
    Never changes the number of newlines."""
    s = unicodedata.normalize("NFC", text).casefold()          # casefold: sharp s -> ss
    s = s.replace("\u00e4", "ae").replace("\u00f6", "oe").replace("\u00fc", "ue")
    s = s.translate(_PUNCT)
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


# ---------------------------------------------------------------- the owner's terms

class TermError(Exception):
    """A rule in the list that cannot be used. Names the rule by number; never quotes it."""

    def __init__(self, index: int, list_line: int, problem: str):
        super().__init__(f"personal term #{index} (list line {list_line}): {problem}")
        self.index = index
        self.list_line = list_line


@dataclass(frozen=True)
class Rule:
    index: int          # 1-based among the rules (comments and empty lines do not count)
    list_line: int      # 1-based line in the list
    pattern: re.Pattern
    is_regex: bool


_WORD = re.compile(r"[^\W_]+")


def _edge_before(ch: str) -> str:
    if ch.isalpha():
        return r"(?<![^\W\d_])"           # no letter right before (a digit or an underscore is fine)
    if ch.isdigit():
        return r"(?<![^\W_])"             # no letter or digit right before
    return ""


def _edge_after(ch: str) -> str:
    if ch.isalpha():
        return r"(?![^\W\d_])"
    if ch.isdigit():
        return r"(?![^\W_])"
    return ""


class TermList:
    def __init__(self, rules: list[Rule]):
        self.rules = rules
        plain = [r for r in rules if not r.is_regex]
        self._any = re.compile("|".join(f"(?:{r.pattern.pattern})" for r in plain), re.I) if plain else None

    @classmethod
    def parse(cls, text: str) -> "TermList":
        rules: list[Rule] = []
        for list_line, raw in enumerate(text.lstrip("\ufeff").split("\n"), 1):
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            index = len(rules) + 1
            if s[:6].lower() == "regex:":
                body = s[6:].strip()
                if not body:
                    raise TermError(index, list_line, "empty regular expression")
                try:
                    rx = re.compile(body, re.I | re.M)
                except re.error:
                    raise TermError(index, list_line, "not a valid regular expression") from None
                for sample in ("", "a", "a b", "1", "-"):
                    if any(m.end() == m.start() for m in rx.finditer(sample)):
                        raise TermError(index, list_line, "the expression matches an empty text, so it would match everywhere")
                rules.append(Rule(index, list_line, rx, True))
            else:
                words = _WORD.findall(fold(s))
                if not words:
                    raise TermError(index, list_line, "no letters or digits in the term")
                pattern = _edge_before(words[0][0]) + r"[\W_]+".join(re.escape(w) for w in words) + _edge_after(words[-1][-1])
                rules.append(Rule(index, list_line, re.compile(pattern, re.I), False))
        return cls(rules)

    def describe(self, index: int) -> str:
        return f"personal term #{index} (list line {self.rules[index - 1].list_line})"

    def find(self, text: str) -> list[tuple[int, int]]:
        """(line, rule index) for every hit, sorted. A term that is a word is not found inside a longer word."""
        if not self.rules or not text:
            return []
        folded = fold(text)
        hits: set[tuple[int, int]] = set()
        if self._any is not None and self._any.search(folded):
            for r in self.rules:
                if not r.is_regex:
                    for m in itertools.islice(r.pattern.finditer(folded), MAX_HITS_PER_RULE):
                        hits.add((_line(folded, m.start()), r.index))
        regexes = [r for r in self.rules if r.is_regex]
        if regexes:
            written = unicodedata.normalize("NFC", text)
            for r in regexes:
                for src in (written, folded):
                    for m in itertools.islice(r.pattern.finditer(src), MAX_HITS_PER_RULE):
                        hits.add((_line(src, m.start()), r.index))
        return sorted(hits)


@dataclass
class TermsSource:
    terms: TermList | None
    origin: str
    warnings: list[str]


def _is_file(p: Path) -> bool:
    try:
        return p.is_file()
    except OSError:
        return False


def load_terms(env: Mapping[str, str], home: Path | None, windows: bool, cli_file: str | None = None) -> TermsSource:
    """The first list that exists and has rules. Raises TermError for a rule that cannot be used."""
    warnings: list[str] = []
    candidates: list[tuple[str, Path, bool]] = []          # label, path, must exist (named on purpose)
    if cli_file:
        candidates.append(("--terms-file", Path(cli_file), True))
    if env.get("KAIRO_PERSONAL_TERMS_FILE"):
        candidates.append(("KAIRO_PERSONAL_TERMS_FILE", Path(env["KAIRO_PERSONAL_TERMS_FILE"]), True))
    data = env.get("KAIRO_DATA_DIR") or (WINDOWS_DATA_DIR if windows else "")
    if data:
        candidates.append(("the data folder", Path(data) / "config" / TERMS_FILE_NAME, False))
    if home is not None:
        candidates.append(("~/.config/kairo", home / ".config" / "kairo" / TERMS_FILE_NAME, False))
    for label, path, named in candidates:
        if not _is_file(path):
            if named:
                warnings.append(f"{label} points to a file that does not exist; looking elsewhere")
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        terms = TermList.parse(text)
        if not terms.rules:
            warnings.append(f"{label}: the list has no terms; looking elsewhere")
            continue
        return TermsSource(terms, f"{len(terms.rules)} rules from {label} ({path})", warnings)
    text = env.get("KAIRO_PERSONAL_TERMS", "")
    if text.strip():
        terms = TermList.parse(text)
        if terms.rules:
            return TermsSource(terms, f"{len(terms.rules)} rules from env KAIRO_PERSONAL_TERMS", warnings)
        warnings.append("KAIRO_PERSONAL_TERMS has no terms")
    return TermsSource(None, "", warnings)


# ---------------------------------------------------------------- forbidden paths

_ENV_EXAMPLES = {".env.example", ".env.sample", ".env.template", ".env.dist"}
_SSH_NAMES = {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
_KEYSTORE_EXT = (".p12", ".pfx", ".jks", ".keystore")
_SQLITE_EXT = (".sqlite", ".sqlite3", ".sqlite-wal", ".sqlite-shm", ".sqlite-journal")


def forbidden_path(path: str) -> list[str]:
    """Why a path must never be committed or shipped (names of rules, empty if it is fine)."""
    p = path.replace("\\", "/")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts:
        return []
    is_dir = p.endswith("/")
    dirs = parts if is_dir else parts[:-1]
    name = "" if is_dir else parts[-1].lower()
    out: list[str] = []
    for d in dirs:
        dl = d.lower()
        for folder in ("private", "data", "devdata"):
            if dl == folder and f"{folder}/ folder" not in out:
                out.append(f"{folder}/ folder")
    if name:
        if fnmatch.fnmatchcase(name, "*.local.*"):
            out.append("*.local.* file")
        if name == ".env" or (name.startswith(".env.") and name not in _ENV_EXAMPLES):
            out.append(".env file")
        if name.endswith(_SQLITE_EXT):
            out.append("*.sqlite database")
        if name.startswith("personal-terms"):
            out.append("personal terms list")
        if fnmatch.fnmatchcase(name, "client_secret*.json"):
            out.append("Google client secret file")
        if name in _SSH_NAMES:
            out.append("ssh private key file")
        if name.endswith(_KEYSTORE_EXT):
            out.append("key store file")
    return out


# ---------------------------------------------------------------- secrets

_PRAGMA = re.compile(r"privacy-scan:\s*allow", re.I)

# Fake values the repo's own tests use: exact values or prefixes. (A line can also carry the allow pragma.)
ALLOWED_FAKE_VALUES: set[str] = set()                     # exact values, for a fake that must stay in the repo
ALLOWED_FAKE_PREFIXES = ("sk-test",)                      # server/tests/test_realtime.py and friends

_PLACEHOLDER = re.compile(
    r"(?<![a-z])(?:test|fake|dummy|example|sample|placeholder|changeme|change[-_]me|redacted|your|insert|replace|"
    r"lorem|ipsum|mock|stub|foobar|not[-_]a[-_]real|todo|xxx+)(?![a-z])", re.I)
_PLACEHOLDER_START = ("test", "fake", "dummy", "example", "sample", "mock")
_SEQUENCE = "abcdefghijklmnopqrstuvwxyz0123456789"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_FILE_VALUE = re.compile(r"\.(?:json|ya?ml|txt|py|js|md|pem|key|cfg|ini|toml|db|sqlite|log|html|css)$", re.I)
_NOT_A_SECRET_KEY = re.compile(r"hash|sha\d*|digest|checksum|fingerprint|public|pubkey", re.I)


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in (s.count(ch) for ch in set(s)))


def _trivial(body: str) -> bool:
    """xxxxxxxx, 00000000, abcdefghij: made up by a person, never a real key."""
    low = body.lower().replace("-", "").replace("_", "")
    return len(set(low)) <= 4 or (len(low) >= 10 and (low in _SEQUENCE or low in _SEQUENCE[::-1]))


def _placeholder(value: str) -> bool:
    low = value.lower()
    return (value in ALLOWED_FAKE_VALUES or low.startswith(tuple(p.lower() for p in ALLOWED_FAKE_PREFIXES))
            or low.startswith(_PLACEHOLDER_START) or value[:1] in "$%" or bool(_PLACEHOLDER.search(value))
            or _trivial(value))


def _random_value(value: str) -> bool:
    """A long value that looks like a generated key, not a word, a name, a path or a placeholder."""
    if len(value) < 20 or _placeholder(value) or _UUID.match(value):
        return False
    if value[:1] in "/~." or "://" in value or _FILE_VALUE.search(value):
        return False
    digits = sum(c.isdigit() for c in value)
    return digits >= 2 and any(c.isalpha() for c in value) and _entropy(value) >= 3.2


def _fake_body(body: str, whole: str) -> bool:
    return whole in ALLOWED_FAKE_VALUES or whole.lower().startswith(tuple(p.lower() for p in ALLOWED_FAKE_PREFIXES)) \
        or _trivial(body)


@dataclass(frozen=True)
class SecretRule:
    name: str
    rx: re.Pattern
    ok: Callable[[re.Match], bool]            # True: it really is a secret
    needles: tuple[str, ...] = ()             # a text without any of these cannot match: skips the regex (speed)
    lower: bool = False                       # the needles are lower case and looked for in the lower-cased text


def _always(_m: re.Match) -> bool:
    return True


_RULES: list[SecretRule] = [
    SecretRule("private key", re.compile(
        r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----(?=(?:(?!-----END)[\s\S]){0,400}?[A-Za-z0-9+/]{24,})"), _always,
        ("PRIVATE KEY",)),
    SecretRule("Google OAuth client secret", re.compile(r"(?<![A-Za-z0-9])GOCSPX-(?P<body>[A-Za-z0-9_\-]{20,})"),
               lambda m: not _fake_body(m["body"], m[0]), ("GOCSPX-",)),
    SecretRule("GitHub token", re.compile(
        r"(?<![A-Za-z0-9])(?:gh[pousr]_(?P<body>[A-Za-z0-9]{30,})|github_pat_(?P<body2>[A-Za-z0-9_]{40,}))"),
               lambda m: not _fake_body(m["body"] or m["body2"], m[0]),
               ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")),
    SecretRule("Tailscale key", re.compile(
        r"(?<![A-Za-z0-9])tskey-(?:[a-z]+-)?(?P<body>[A-Za-z0-9]{8,}(?:-[A-Za-z0-9]{8,})?)"),
               lambda m: not _fake_body(m["body"], m[0]), ("tskey-",)),
    SecretRule("API key (sk-)", re.compile(r"(?<![A-Za-z0-9_])sk-(?P<body>[A-Za-z0-9_\-]{20,})"),
               lambda m: not _fake_body(m["body"], m[0]) and any(c.isdigit() for c in m["body"])
               and any(c.isalpha() for c in m["body"]) and not _PLACEHOLDER.search(m["body"]), ("sk-",)),
    SecretRule("AWS access key", re.compile(r"(?<![A-Z0-9])(?P<body>(?:AKIA|ASIA)[0-9A-Z]{16})(?![A-Z0-9])"),
               lambda m: not m[0].endswith("EXAMPLE") and not _trivial(m["body"][4:]), ("AKIA", "ASIA")),
    SecretRule("Google API key", re.compile(r"(?<![A-Za-z0-9_\-])AIza(?P<body>[0-9A-Za-z_\-]{35})(?![0-9A-Za-z_\-])"),
               lambda m: not _fake_body(m["body"], m[0]), ("AIza",)),
    SecretRule("Google OAuth token", re.compile(
        r"(?<![A-Za-z0-9])(?:ya29\.|1//0)(?P<body>[A-Za-z0-9_\-]{30,})"), lambda m: not _trivial(m["body"]),
               ("ya29.", "1//0")),
    SecretRule("Slack token", re.compile(r"(?<![A-Za-z0-9])xox[abprs]-(?P<body>[A-Za-z0-9\-]{10,})"),
               lambda m: not _trivial(m["body"]), ("xox",)),
    SecretRule("Stripe key", re.compile(r"(?<![A-Za-z0-9])[sr]k_live_(?P<body>[0-9a-zA-Z]{20,})"),
               lambda m: not _trivial(m["body"]), ("_live_",)),
    SecretRule("npm token", re.compile(r"(?<![A-Za-z0-9])npm_(?P<body>[A-Za-z0-9]{36})(?![A-Za-z0-9])"),
               lambda m: not _trivial(m["body"]), ("npm_",)),
    SecretRule("JSON web token", re.compile(
        r"(?<![A-Za-z0-9_\-])eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.(?P<body>[A-Za-z0-9_\-]{10,})"), _always, ("eyJ",)),
    SecretRule("bearer token", re.compile(r"(?<![A-Za-z0-9])bearer[ \t]+(?P<body>[A-Za-z0-9._~+/\-]{30,}=*)", re.I),
               lambda m: _random_value(m["body"]), ("bearer",), True),
]

_GENERIC = re.compile(
    r"(?P<kw>password|passwd|passphrase|secret|token|api[_\-]?key|apikey|private[_\-]?key|auth[_\-]?key|access[_\-]?key|"
    r"credential)(?P<tail>[A-Za-z0-9_\-]{0,40})[\"'`]?[ \t]*(?::=|=>|[:=])[ \t]*[\"'`]?"
    r"(?P<body>[A-Za-z0-9_\-+/=.~!@#$%^&*?:]{20,})(?![\w(\[{])", re.I)
GENERIC_NAME = "password/secret/token assignment"
_GENERIC_NEEDLES = ("password", "passwd", "passphrase", "secret", "token", "api_key", "api-key", "apikey", "private_key",
                    "private-key", "privatekey", "auth_key", "auth-key", "authkey", "access_key", "access-key",
                    "accesskey", "credential")


def scan_secrets(text: str) -> list[tuple[int, str]]:
    """(line, rule name) for every secret in the text, sorted. Never returns the value."""
    found: dict[int, list[str]] = {}

    def line_text(pos: int) -> str:
        start = text.rfind("\n", 0, pos) + 1
        end = text.find("\n", pos)
        return text[start: len(text) if end < 0 else end]

    def add(pos: int, name: str) -> None:
        if _PRAGMA.search(line_text(pos)):
            return
        names = found.setdefault(_line(text, pos), [])
        if name not in names:
            names.append(name)

    low = text.lower()
    for rule in _RULES:
        hay = low if rule.lower else text
        if rule.needles and not any(n in hay for n in rule.needles):
            continue
        for m in rule.rx.finditer(text):
            if rule.ok(m):
                add(m.start(), rule.name)
    if any(k in low for k in _GENERIC_NEEDLES):
        for m in _GENERIC.finditer(text):
            start = text.rfind("\n", 0, m.start()) + 1
            key = re.search(r"[A-Za-z0-9_\-]*$", text[max(start, m.start() - 40): m.start()])[0] + m["kw"] + m["tail"]
            if not _NOT_A_SECRET_KEY.search(key) and _random_value(m["body"]):
                if _line(text, m.start()) not in found:             # a specific rule already named it
                    add(m.start(), GENERIC_NAME)
    return sorted((line, name) for line, names in found.items() for name in names)


# ---------------------------------------------------------------- reporting

@dataclass(frozen=True)
class Finding:
    where: str
    line: int | None
    rule: str
    extra: str = ""

    def text(self) -> str:
        loc = self.where + (f":{self.line}" if self.line else "")
        return f"{loc}: {self.rule}" + (f" [{self.extra}]" if self.extra else "")


class Report:
    def __init__(self) -> None:
        self.findings: dict[Finding, None] = {}            # ordered, no duplicates
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.items = 0

    def add(self, f: Finding) -> None:
        self.findings.setdefault(f)


class GitError(Exception):
    pass


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_PAGER="cat")
    return env


def _git(repo: Path | str, *args: str, stdin: bytes | None = None) -> bytes:
    try:
        p = subprocess.run(["git", "-c", "core.quotepath=off", "-C", str(repo), *args], input=stdin,
                           capture_output=True, env=_git_env())
    except FileNotFoundError:
        raise GitError("git is not installed or not on PATH") from None
    if p.returncode != 0:
        why = p.stderr.decode("utf-8", "replace").strip().splitlines()
        raise GitError(f"git {args[0]} failed" + (f": {why[-1][:200]}" if why else ""))
    return p.stdout


def _dec(b: bytes) -> str:
    return b.decode("utf-8", "surrogateescape")


def _decodings(data: bytes, binary: bool) -> Iterator[str]:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        yield data.decode("utf-16", errors="replace")
        return
    try:
        yield data.decode("utf-8-sig")
    except UnicodeDecodeError:
        yield data.decode("utf-8", errors="replace")
        if not binary:
            yield data.decode("latin-1")                       # old Windows files: the umlauts are single bytes


class Scanner:
    def __init__(self, terms: TermList | None, secrets: bool = True) -> None:
        self.terms = terms
        self.secrets = secrets
        self.report = Report()
        self._safe_cache: dict[str, str] = {}
        self._archive_budget = MAX_ZIP_TOTAL

    # a name that holds a term is never printed
    def safe(self, raw: str) -> str:
        shown = self._safe_cache.get(raw)
        if shown is None:
            shown = raw
            if self.terms and self.terms.find(raw):
                digest = hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:8]
                shown = f"<redacted name {digest}>"
            self._safe_cache[raw] = shown
        return shown

    def add(self, where: str, line: int | None, rule: str, extra: str = "") -> None:
        self.report.add(Finding(where, line, rule, extra))

    def check_path(self, path: str, prefix: str = "", extra: str = "") -> None:
        where = prefix + self.safe(path)
        for name in forbidden_path(path):
            self.add(where, None, f"forbidden path: {name}", extra)
        if self.terms:
            for _line_no, idx in self.terms.find(path):
                self.add(where, None, self.terms.describe(idx) + " in the name", extra)

    def scan_text(self, where: str, text: str, only_lines: set[int] | None = None, extra: str = "",
                  one_line: bool = False) -> None:
        hits: list[tuple[int, str]] = []
        if self.terms:
            hits += [(ln, self.terms.describe(idx)) for ln, idx in self.terms.find(text)]
        if self.secrets:
            hits += [(ln, f"secret: {name}") for ln, name in scan_secrets(text)]
        if only_lines is not None:
            hits = [h for h in hits if h[0] in only_lines]
        hits.sort()
        for ln, rule in hits[:MAX_FINDINGS_PER_FILE]:
            self.add(where, None if one_line else ln, rule, extra)
        if len(hits) > MAX_FINDINGS_PER_FILE:
            self.add(where, None, f"{len(hits) - MAX_FINDINGS_PER_FILE} more finding(s) in this file", extra)

    def scan_blob(self, path: str, data: bytes, *, only_lines: set[int] | None = None, extra: str = "",
                  depth: int = 0, path_checked: bool = False) -> None:
        self.report.items += 1
        if not path_checked:
            self.check_path(path, extra=extra)
        where = self.safe(path)
        if len(data) > MAX_BYTES:
            self.report.warnings.append(f"{where}: {len(data) // 2 ** 20} MB, too big to scan; only its name was checked")
            return
        if data[:4] == b"PK\x03\x04" and depth < MAX_ZIP_DEPTH:
            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile:
                zf = None
            if zf is not None:
                with zf:
                    self.scan_zip(zf, path + "!/", extra=extra, depth=depth + 1)
                return
        binary = b"\x00" in data[:8192]
        if binary and PurePosixPath(path.lower()).suffix in SKIP_BINARY_EXT:
            return
        for text in _decodings(data, binary):
            self.scan_text(where, text, only_lines, extra)

    def scan_zip(self, zf: zipfile.ZipFile, prefix: str = "", extra: str = "", depth: int = 0) -> None:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_MEMBERS:
            self.report.warnings.append(f"{self.safe(prefix) or 'archive'}: {len(infos)} entries, only the first {MAX_ZIP_MEMBERS} are checked")
            infos = infos[:MAX_ZIP_MEMBERS]
        for info in infos:
            full = prefix + info.filename
            self.check_path(full, extra=extra)
            if info.filename.endswith("/"):
                continue
            if info.file_size > MAX_BYTES or info.file_size > self._archive_budget:
                self.report.warnings.append(f"{self.safe(full)}: too big to scan; only its name was checked")
                continue
            try:
                data = zf.read(info)
            except (RuntimeError, zipfile.BadZipFile, NotImplementedError, OSError) as exc:
                self.report.warnings.append(f"{self.safe(full)}: could not be read ({type(exc).__name__})")
                continue
            self._archive_budget -= len(data)
            self.scan_blob(full, data, extra=extra, depth=depth, path_checked=True)

    # ------------------------------------------------------------ modes

    def scan_zip_file(self, path: Path) -> None:
        try:
            zf = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as exc:
            self.report.errors.append(f"{path.name}: not a readable zip ({type(exc).__name__})")
            return
        with zf:
            self.scan_zip(zf)

    def scan_tree(self, root: Path) -> None:
        for rel in _tree_files(root):
            full = root / rel
            try:
                if full.is_symlink() or not full.is_file():
                    continue
                size = full.stat().st_size
                if size > MAX_BYTES:
                    self.check_path(rel)
                    self.report.warnings.append(f"{self.safe(rel)}: {size // 2 ** 20} MB, too big to scan; only its name was checked")
                    continue
                data = full.read_bytes()
            except OSError as exc:
                self.report.warnings.append(f"{self.safe(rel)}: could not be read ({type(exc).__name__})")
                continue
            self.scan_blob(rel, data)

    def scan_staged(self, repo: Path) -> None:
        names = _git(repo, "diff", "--cached", "--numstat", "-z", "--no-renames", "--diff-filter=ACMRT").split(b"\0")
        for rec in names:
            if not rec:
                continue
            added, _deleted, path_b = _dec(rec).split("\t", 2)
            path = path_b
            self.check_path(path)
            if added == "-":                                         # binary: the whole file counts
                self.scan_blob(path, _git(repo, "cat-file", "blob", f":{path}"), path_checked=True)
                continue
            diff = _git(repo, "diff", "--cached", "-U0", "--no-color", "--no-ext-diff", "--no-renames",
                        "--", f":(literal){path}")
            lines = _added_lines(_dec(diff))
            if not lines:
                continue
            self.scan_blob(path, _git(repo, "cat-file", "blob", f":{path}"), only_lines=lines, path_checked=True)

    def scan_message_file(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8", errors="replace")
        text = "\n".join("" if ln.startswith("#") else ln for ln in text.split("\n"))     # git's own comment lines
        self.report.items += 1
        self.scan_text("commit message", text)

    def scan_history(self, repo: Path, refs: list[str], ci: bool) -> None:
        for r in refs:
            if r.startswith("-") and r != "--all":
                raise GitError("a ref cannot start with a dash")
        spec = refs or ["--all"]
        shallow = _dec(_git(repo, "rev-parse", "--is-shallow-repository")).strip() == "true"
        if shallow:
            msg = "this is a shallow clone: only the commits it has are scanned (CI needs fetch-depth: 0)"
            (self.report.errors if ci else self.report.warnings).append(msg)
        # 1. every object reachable from the refs, with the first path it was seen under
        objects: dict[str, str] = {}
        for ln in _git(repo, "rev-list", "--objects", *spec).split(b"\n"):
            if ln:
                sha, _, path = _dec(ln).partition(" ")
                objects.setdefault(sha, path)
        types = self._object_types(repo, list(objects))
        # 2. the contents of blobs and annotated tags, each once
        want = [sha for sha, (typ, size) in types.items() if typ in ("blob", "tag")]
        paths_seen: set[str] = set()
        flagged: list[str] = []
        before = len(self.report.findings)
        for sha, typ, data in self._read_objects(repo, want, types):
            path = objects.get(sha) or ""
            label = path if typ == "blob" and path else f"annotated tag {sha[:7]}"
            if data is None:
                self.report.warnings.append(f"{self.safe(label)}: too big to scan; only its name was checked")
                continue
            if typ == "blob" and path:
                paths_seen.add(path)
            mark = len(self.report.findings)
            self.scan_blob(label, data, extra=f"blob {sha[:7]}", path_checked=True)
            if len(self.report.findings) > mark:
                flagged.append(sha)
        # 3. every path ever touched, for the forbidden-path and term-in-name checks
        names = _git(repo, "log", "--name-only", "--format=", "-z", "--no-renames", *spec).split(b"\0")
        for n in names:
            if n:
                paths_seen.add(_dec(n).strip("\n"))
        for p in sorted(paths_seen):
            self.check_path(p)
        # 4. commit messages and identities
        fmt = "%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e"
        for rec in _dec(_git(repo, "log", f"--format={fmt}", *spec)).split("\x1e"):
            rec = rec.lstrip("\n")
            if not rec:
                continue
            sha, an, ae, cn, ce, body = rec.split("\x1f", 5)
            self.report.items += 1
            who = f"commit {sha[:7]}"
            for what, value in (("author name", an), ("author email", ae), ("committer name", cn), ("committer email", ce)):
                self.scan_text(f"{who} {what}", value, one_line=True)
            self.scan_text(f"{who} message", body.rstrip("\n"))
        # 5. ref names
        for ref in _dec(_git(repo, "for-each-ref", "--format=%(refname)")).split("\n"):
            if ref:
                self.check_path(ref, prefix="ref ")
        # 6. which commit brought each flagged blob in (only for the first few)
        if flagged:
            self._name_commits(repo, flagged[:25], spec, before)

    def _object_types(self, repo: Path, shas: list[str]) -> dict[str, tuple[str, int]]:
        out: dict[str, tuple[str, int]] = {}
        if not shas:
            return out
        with tempfile.TemporaryFile() as f:
            f.write(("\n".join(shas) + "\n").encode())
            f.seek(0)
            p = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
                               stdin=f, capture_output=True, env=_git_env())
        if p.returncode != 0:
            raise GitError("git cat-file failed")
        for ln in p.stdout.decode("utf-8", "replace").splitlines():
            parts = ln.split()
            if len(parts) == 3 and parts[2].isdigit():
                out[parts[0]] = (parts[1], int(parts[2]))
        return out

    def _read_objects(self, repo: Path, shas: list[str], types: dict[str, tuple[str, int]]
                      ) -> Iterator[tuple[str, str, bytes | None]]:
        if not shas:
            return
        with tempfile.TemporaryFile() as f:
            f.write(("\n".join(shas) + "\n").encode())
            f.seek(0)
            proc = subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"], stdin=f, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, env=_git_env())
            try:
                out = proc.stdout
                while True:
                    header = out.readline()
                    if not header:
                        break
                    parts = header.split()
                    if len(parts) != 3:
                        continue                                      # "<sha> missing"
                    sha, typ, size = parts[0].decode(), parts[1].decode(), int(parts[2])
                    data = out.read(size)
                    out.read(1)                                       # the newline after the content
                    yield sha, typ, (data if size <= MAX_BYTES else None)
            finally:
                proc.stdout.close()
                proc.wait()

    def _name_commits(self, repo: Path, shas: list[str], spec: list[str], before: int) -> None:
        by_blob: dict[str, str] = {}
        for sha in shas:
            try:
                out = _dec(_git(repo, "log", "--format=%h", "-n", "3", f"--find-object={sha}", *spec)).split()
            except GitError:
                continue
            if out:
                by_blob[sha[:7]] = ", ".join(out)
        if not by_blob:
            return
        rebuilt: dict[Finding, None] = {}
        for i, f in enumerate(self.report.findings):
            if i >= before and f.extra.startswith("blob "):
                commits = by_blob.get(f.extra[5:12])
                if commits:
                    f = Finding(f.where, f.line, f.rule, f"{f.extra}; commit {commits}")
            rebuilt.setdefault(f)
        self.report.findings = rebuilt


def _added_lines(diff: str) -> set[int]:
    """New-file line numbers of the added lines in a -U0 diff of one file."""
    added: set[int] = set()
    lines = diff.split("\n")
    i = 0
    while i < len(lines):
        m = re.match(r"@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", lines[i])
        i += 1
        if not m:
            continue
        old_n = int(m.group(1)) if m.group(1) is not None else 1
        new_start = int(m.group(2))
        new_n = int(m.group(3)) if m.group(3) is not None else 1
        new_no = new_start
        while (old_n > 0 or new_n > 0) and i < len(lines):
            ln = lines[i]
            i += 1
            if ln.startswith("+"):
                added.add(new_no)
                new_no += 1
                new_n -= 1
            elif ln.startswith("-"):
                old_n -= 1
            elif ln.startswith("\\"):
                continue
            else:                                                    # a context line (not with -U0, but harmless)
                old_n -= 1
                new_n -= 1
                new_no += 1
    return added


def _tree_files(root: Path) -> Iterator[str]:
    """Files under root as relative posix paths. In the top folder of a git work tree: what git would commit
    (tracked, or new and not ignored). Anywhere else: everything except .git folders."""
    try:
        top = _dec(_git(root, "rev-parse", "--show-toplevel")).strip()
        in_git = bool(top) and os.path.samefile(top, root)
    except (GitError, OSError):
        in_git = False
    if in_git:
        for rel in _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0"):
            if rel:
                yield _dec(rel)
        return
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(files):
            yield (Path(base) / name).relative_to(root).as_posix()


# ---------------------------------------------------------------- command line

def _escape_property(s: str) -> str:
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A").replace(":", "%3A").replace(",", "%2C")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="privacy_scan.py", description="Personal data and secrets must never get into the repo or a release.")
    p.add_argument("--staged", action="store_true", help="the lines the next commit adds")
    p.add_argument("--tree", nargs="?", const=".", metavar="DIR", help="a directory tree (default: .)")
    p.add_argument("--history", nargs="*", metavar="REF", help="every commit reachable from the refs (default: all refs)")
    p.add_argument("--zip", nargs="+", metavar="FILE", help="a release zip")
    p.add_argument("--message-file", metavar="FILE", help="a commit message (the commit-msg hook)")
    p.add_argument("--ci", action="store_true", help="GitHub Actions: annotations; a missing list on main is an error")
    p.add_argument("--terms-file", metavar="FILE", help="the personal terms list (else see the module's help)")
    p.add_argument("--repo", metavar="DIR", default=".", help="the git repository for --staged and --history (default: .)")
    return p


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")       # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    env = os.environ if env is None else env
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not (args.staged or args.tree is not None or args.history is not None or args.zip or args.message_file):
        parser.error("choose what to scan: --staged, --tree, --history, --zip or --message-file")

    try:
        home: Path | None = Path.home()
    except (RuntimeError, KeyError):
        home = None
    try:
        source = load_terms(env, home, os.name == "nt", args.terms_file)
    except TermError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: the personal-terms list could not be read ({exc.strerror or type(exc).__name__})", file=sys.stderr)
        return 2

    scanner = Scanner(source.terms)
    report = scanner.report
    config_errors: list[str] = []
    notes = list(source.warnings)
    on_main = env.get("GITHUB_REF") == "refs/heads/main"
    if source.terms is None:
        where = (f"{WINDOWS_DATA_DIR}\\config\\{TERMS_FILE_NAME}" if os.name == "nt"
                 else f"~/.config/kairo/{TERMS_FILE_NAME}")
        if args.ci and on_main:
            config_errors.append("no personal-terms list found, so the personal-terms check could not run. Add the "
                                 "repository secret PERSONAL_TERMS (one term per line) and pass it to this step as the "
                                 "environment variable KAIRO_PERSONAL_TERMS")
        else:
            notes.append("no personal-terms list found: personal terms were NOT checked (secrets and paths were). "
                         f"Put one term per line in {where}, or point KAIRO_PERSONAL_TERMS_FILE at a list")

    try:
        if args.staged:
            scanner.scan_staged(Path(args.repo))
        if args.message_file:
            scanner.scan_message_file(Path(args.message_file))
        if args.tree is not None:
            root = Path(args.tree)
            if not root.is_dir():
                config_errors.append(f"--tree: {root} is not a directory")
            else:
                scanner.scan_tree(root)
        if args.history is not None:
            scanner.scan_history(Path(args.repo), args.history, args.ci)
        for z in args.zip or []:
            scanner.scan_zip_file(Path(z))
    except GitError as exc:
        config_errors.append(str(exc))
    except OSError as exc:
        config_errors.append(f"could not read a file ({exc.strerror or type(exc).__name__})")

    config_errors += report.errors
    findings = sorted(report.findings, key=lambda f: (f.where, f.line or 0, f.rule, f.extra))
    for w in notes + report.warnings:
        print(f"warning: {w}", file=sys.stderr)
    for e in config_errors:
        print(f"error: {e}", file=sys.stderr)
    for f in findings[:MAX_LINES_PRINTED]:
        print(f.text())
    if len(findings) > MAX_LINES_PRINTED:
        print(f"... and {len(findings) - MAX_LINES_PRINTED} more")
    if args.ci and env.get("GITHUB_ACTIONS") == "true":
        for f in findings[:MAX_ANNOTATIONS]:
            if f.line:
                print(f"::error file={_escape_property(f.where)},line={f.line}::{_escape_property(f.rule)}")
            else:
                print(f"::error::{_escape_property(f.where + ': ' + f.rule)}")
        for e in config_errors:
            print(f"::error::{_escape_property(e)}")
    terms_note = f"personal terms: {source.origin}" if source.terms else "personal terms: not checked"
    if findings:
        print(f"privacy_scan: {len(findings)} finding(s) ({terms_note})")
        return 1
    if config_errors:
        print(f"privacy_scan: could not finish ({terms_note})")
        return 2
    print(f"privacy_scan: clean, {report.items} item(s) checked ({terms_note})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
