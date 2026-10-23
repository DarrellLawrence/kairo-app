#!/usr/bin/env python3
"""Keep personal data and secrets out of the repo and out of a release (owner's master prompt 0.6).

    python scripts/privacy_scan.py --staged                  # what a commit adds (the pre-commit hook)
    python scripts/privacy_scan.py --tree [DIR]              # a directory (default: the current one)
    python scripts/privacy_scan.py --history [REF ...]       # every commit reachable from the refs (default: all refs):
                                                             #   file contents, commit objects, messages, identities, ref names
    python scripts/privacy_scan.py --zip dist/Kairo-Setup-*.zip
    python scripts/privacy_scan.py --message-file FILE       # a commit message (the commit-msg hook)
    python scripts/privacy_scan.py --gitleaks-report FILE    # print a gitleaks JSON report without any matched text
    python scripts/privacy_scan.py --install-hooks           # hooks that also protect old branches and worktrees
    add --ci in GitHub Actions (annotations; a missing terms list on main is an error)
    add --secrets-only to skip the terms check on purpose (the step has no terms; another job checks them)

Two detectors, plus a list of paths that must never be committed:

1. The owner's personal TERMS (names, places, numbers ...), read from a file OUTSIDE the repo. The first of
   these that exists wins:
     --terms-file FILE
     env KAIRO_PERSONAL_TERMS_FILE
     <data dir>/config/personal-terms.txt  (data dir = env KAIRO_DATA_DIR; on Windows C:\\Kairo\\data)
     ~/.config/kairo/personal-terms.txt
     env KAIRO_PERSONAL_TERMS             (the same list as text, for CI from a GitHub secret)
   A list that was named on purpose (--terms-file, KAIRO_PERSONAL_TERMS_FILE) and is missing is an error.
   One rule per line (UTF-8 or UTF-16 with a BOM; a Windows-1252 file is read with a note). Lines starting with # and
   empty lines are ignored. A line is a term (words, any case; a u-umlaut also matches ue and u, ss = sharp s; no or
   any punctuation between the words, so first.last, FirstLast and "Last, First" are found; never inside a longer
   word; a phone number written with 0 also matches +41 ...) or "regex:<pattern>" (case-insensitive; it sees the text
   as written and with umlauts folded). A text is also read after \\uXXXX, \\xFC, %C3%BC, &uuml;, =C3=BC, RFC 2047 and
   base64 decoding. A matched term is NEVER printed, not in a log, not in CI: only file:line and the rule number are.
   Without a list the check is skipped with a warning (exit 0), but with --ci on refs/heads/main it is an error
   (exit 2), so the missing secret is noticed.
2. Secrets: private key blocks, Google/GitHub/Tailscale/OpenAI/AWS/Slack/Stripe keys, JSON web tokens, VAPID and
   JWK private keys and password/secret/token/key assignments with long random values. Known fake values and
   placeholders are allowed; in server/tests, docs and installer/ci a line can carry "privacy-scan: allow" for a
   deliberate fake. Only the rule's name is printed.
3. Paths: private/, secrets/, vault/, data/, devdata/ folders, *.local.* files, .env and *.env files (not examples),
   databases (by name and by their SQLite header), the terms list, key stores, Google client secrets, Kairo's own
   secret stores (by their header), documents and photos (PDF, Office, JPEG ...) outside web/public.

Everything is read inside archives too: zip, gzip, bzip2, xz and tar (any depth up to MAX_ARCHIVE_DEPTH), office files,
PNG text chunks, PDF streams, binary strings (latin-1, UTF-8, UTF-16). What cannot be read (too big, encrypted, 7z,
rar, zstd, damaged) is reported as "not scanned": a warning, but an error (exit 2) with --ci, --zip or --strict.

Exit codes: 0 clean, 1 findings, 2 could not do the check (bad list, no list on main in CI, bad zip, shallow clone in
CI, something that could not be scanned in strict mode).

Standard library only (Python 3.12, the project's version); Windows and Linux.
"""

from __future__ import annotations

import argparse
import base64
import bisect
import bz2
import codecs
import fnmatch
import functools
import gzip
import html
import io
import itertools
import json
import lzma
import math
import os
import quopri
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
import zipfile
import zlib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

TERMS_FILE_NAME = "personal-terms.txt"
WINDOWS_DATA_DIR = "C:\\Kairo\\data"

MAX_BYTES = 256 * 1024 * 1024           # one file, blob or archive member; a bigger one is "not scanned", never silent
CHUNK_BYTES = 16 * 1024 * 1024          # a big text is scanned in pieces of this size
CHUNK_OVERLAP = 2048                    # ... that overlap, so a name over a cut is found
MAX_ARCHIVE_DEPTH = 8                   # archives inside archives (a .docx is one)
MAX_ARCHIVE_MEMBERS = 100_000
MAX_ARCHIVE_TOTAL = 4 * 1024 ** 3       # decompressed bytes read from archives in one run
MAX_DECODED_BYTES = 16 * 1024 * 1024    # base64 / stream decoding inside one text
MAX_FINDINGS_PER_FILE = 25
MAX_HITS_PER_RULE = 200
MAX_LINES_PRINTED = 300
MAX_ANNOTATIONS = 10                    # GitHub shows 10 per step

# "privacy-scan: allow" is only honoured in these places (a pull request cannot excuse a real secret elsewhere).
PRAGMA_DIRS = ("server/tests/", "docs/", "installer/ci/")

# Documents and photos hold personal data in places no word list can read (scans, EXIF, glyph ids): only these
# folders may contain them. (The app's own icons are PNG and are read as binary strings like everything else.)
DOCUMENT_ALLOWED_DIRS = ("web/public/", "web/dist/")
DOCUMENT_EXT = {
    ".pdf", ".jpg", ".jpeg", ".jpe", ".jfif", ".heic", ".heif", ".tif", ".tiff", ".doc", ".docx", ".docm", ".xls",
    ".xlsx", ".xlsm", ".ppt", ".pptx", ".odt", ".ods", ".odp", ".rtf", ".pst", ".ost", ".mbox", ".msg",
}


# ---------------------------------------------------------------- folding

_PUNCT_MAP = {"\u2019": "'", "\u2018": "'", "\u00b4": "'", "\u2013": "-", "\u2014": "-", "\u2010": "-", "\u2011": "-"}
_PUNCT_RX = re.compile("[\u2019\u2018\u00b4\u2013\u2014\u2010\u2011]")
# Cyrillic and Greek letters that look like Latin ones (a name written with one of them is still the name)
_CONFUSABLES = {"\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c", "\u0445": "x", "\u0443": "y",
                "\u0456": "i", "\u0458": "j", "\u04bb": "h", "\u0455": "s", "\u03bf": "o", "\u03b1": "a", "\u03c1": "p",
                "\u03b9": "i", "\u03ba": "k", "\u03bd": "v", "\u03c5": "u"}
_CONFUSABLES_RX = re.compile("[" + "".join(_CONFUSABLES) + "]")
_FILLERS = frozenset("\u115f\u1160\u3164\uffa0\u2800")      # Hangul and Braille fillers: letters that show nothing
_ASCII_CONTROLS = re.compile("[\x00-\x08\x0e-\x1b\x7f]")
_CONTROLS = re.compile("[\x00-\x08\x0e-\x1b\x7f-\x84\x86-\x9f]")    # controls that are not white space


@functools.lru_cache(maxsize=None)
def _shows_nothing(ch: str) -> bool:
    cat = unicodedata.category(ch)
    return cat in ("Cf", "Mn", "Me") or (cat == "Cc" and not ch.isspace()) or ch in _FILLERS


def _drop_invisible(s: str) -> str:
    """s without what shows nothing inside a word: format characters (zero-width, direction marks, tags, the byte order
    mark ...), combining and enclosing marks (accents after NFKD, variation selectors), controls that are not white
    space and the fillers. White space stays, it separates words. Only the characters s holds are looked up."""
    bad = [c for c in set(s) if _shows_nothing(c)]
    return re.sub("[" + "".join(map(re.escape, bad)) + "]", "", s) if bad else s


def fold(text: str) -> str:
    """Lower case, umlauts as their two-letter form (ue, oe, ae, ss), other accents dropped, quotes and dashes plain,
    invisible characters gone. Never changes the number of newlines."""
    if text.isascii():
        return _ASCII_CONTROLS.sub("", text.lower())
    s = text if unicodedata.is_normalized("NFC", text) else unicodedata.normalize("NFC", text)
    s = s.replace("\u0345", "")                     # a combining mark that casefold would turn into a letter
    s = s.casefold().replace("\u00e4", "ae").replace("\u00f6", "oe").replace("\u00fc", "ue")     # casefold: sharp s -> ss
    if not s.isascii():
        s = _PUNCT_RX.sub(lambda m: _PUNCT_MAP[m[0]], s)
        s = _CONFUSABLES_RX.sub(lambda m: _CONFUSABLES[m[0]], s)
        s = _drop_invisible(unicodedata.normalize("NFKD", s))
    return _ASCII_CONTROLS.sub("", s)


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


# ---------------------------------------------------------------- the same text, decoded
#
# A name hides well in JSON ("M\u00fcller"), a URL (first%20last), HTML (M&uuml;ller), a mail
# (M=C3=BCller, =?utf-8?B?...?=) or base64. Every text is therefore also looked at decoded. A view is
# (text, line map): the map sends a line of the view back to a line of the original (None: the same lines).

View = tuple[str, "list[int] | None"]

_ESC_TRIGGER = re.compile(r"\\[uUxN]|%[0-9A-Fa-f]{2}|&#?[A-Za-z0-9]{1,32};|=[0-9A-Fa-f]{2}|=\?[\w\-]{1,40}\?[bBqQ]\?")
_UNSAFE_CHARS = str.maketrans({c: " " for c in "\n\r\x0b\x0c\x85\u2028\u2029\x00"})
_RX_U_PAIR = re.compile(r"\\u([dD][89abAB][0-9a-fA-F]{2})\\u([dD][c-fC-F][0-9a-fA-F]{2})")
_RX_U_BRACE = re.compile(r"\\u\{([0-9a-fA-F]{1,6})\}")
_RX_U4 = re.compile(r"\\u([0-9a-fA-F]{4})")
_RX_U8 = re.compile(r"\\U([0-9a-fA-F]{8})")
_RX_X_RUN = re.compile(r"(?:\\x[0-9a-fA-F]{2})+")
_RX_NAMED = re.compile(r"\\N\{([^}\n]{1,80})\}")
_RX_BSLASH_WS = re.compile(r"\\[nrt]")
_RX_PCT_RUN = re.compile(r"(?:%[0-9a-fA-F]{2})+")
_RX_QP_RUN = re.compile(r"(?:=[0-9A-Fa-f]{2})+")
_RX_ENTITY = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
_RX_RFC2047 = re.compile(r"=\?([A-Za-z0-9_\-]{1,40})\?([bBqQ])\?([^?\s]*)\?=")
_RX_B64 = re.compile(r"(?<![A-Za-z0-9+/_\-=])[A-Za-z0-9+/_\-]{16,}={0,2}(?![A-Za-z0-9+/_\-])")


def _san(s: str) -> str:
    return s.translate(_UNSAFE_CHARS)


def _chr(code: int) -> str:
    try:
        return _san(chr(code))
    except (ValueError, OverflowError):
        return ""


def _bytes_text(b: bytes) -> str:
    try:
        return _san(b.decode("utf-8"))
    except UnicodeDecodeError:
        return _san(b.decode("latin-1"))


def _rfc2047(m: re.Match) -> str:
    charset, enc, payload = m[1], m[2].lower(), m[3]
    try:
        if enc == "b":
            raw = base64.b64decode(payload + "=" * (-len(payload) % 4))
        else:
            raw = quopri.decodestring(payload.replace("_", " ").encode("ascii", "ignore"))
        try:
            return _san(raw.decode(codecs.lookup(charset).name, "replace"))
        except LookupError:
            return _san(raw.decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return m[0]


def _named(m: re.Match) -> str:
    try:
        return _san(unicodedata.lookup(m[1]))
    except KeyError:
        return m[0]


def _unescape(text: str) -> str:
    """JSON/JS/Python escapes, URL encoding, HTML entities, quoted-printable and RFC 2047 words, decoded. Up to three
    layers (a URL that holds a JSON string ...). Newlines are never created."""
    for _ in range(3):
        if not _ESC_TRIGGER.search(text):
            break
        s = text
        s = _RX_RFC2047.sub(_rfc2047, s)
        s = _RX_U_PAIR.sub(lambda m: _chr(0x10000 + ((int(m[1], 16) - 0xD800) << 10) + (int(m[2], 16) - 0xDC00)), s)
        s = _RX_U_BRACE.sub(lambda m: _chr(int(m[1], 16)), s)
        s = _RX_U4.sub(lambda m: _chr(int(m[1], 16)), s)
        s = _RX_U8.sub(lambda m: _chr(int(m[1], 16)), s)
        s = _RX_X_RUN.sub(lambda m: _bytes_text(bytes.fromhex(m[0].replace("\\x", ""))), s)
        s = _RX_NAMED.sub(_named, s)
        s = _RX_BSLASH_WS.sub(" ", s)
        s = _RX_PCT_RUN.sub(lambda m: _bytes_text(bytes.fromhex(m[0].replace("%", ""))), s)
        s = _RX_QP_RUN.sub(lambda m: _bytes_text(bytes.fromhex(m[0].replace("=", ""))), s)
        s = _RX_ENTITY.sub(lambda m: _san(html.unescape(m[0])), s)
        if s == text:
            break
        text = s
    return text


def _qp_join(text: str) -> tuple[str, list[int]] | None:
    """Quoted-printable soft line breaks ("=" at the end of a line) joined; the map keeps the original line numbers."""
    if "=\n" not in text and "=\r\n" not in text:
        return None
    if not _RX_QP_RUN.search(text):
        return None
    lines = text.split("\n")
    out: list[str] = []
    numbers: list[int] = []
    i = 0
    while i < len(lines):
        cur, start = lines[i], i + 1
        while cur.rstrip("\r").endswith("=") and i + 1 < len(lines):
            cur = cur.rstrip("\r")[:-1]
            i += 1
            cur += lines[i]
        out.append(cur)
        numbers.append(start)
        i += 1
    return "\n".join(out), numbers


def _printable_share(s: str) -> float:
    if not s:
        return 0.0
    return sum(1 for c in s if c.isprintable() or c in "\t\r\n") / len(s)


def _try_base64(run: str) -> str | None:
    s = run.rstrip("=").replace("-", "+").replace("_", "/")
    if len(s) % 4 == 1:
        return None
    try:
        raw = base64.b64decode(s + "=" * (-len(s) % 4))
    except ValueError:
        return None
    if len(raw) < 8:
        return None
    for enc in ("utf-8", "utf-16-le"):
        if enc == "utf-16-le" and raw.count(0) < len(raw) // 4:
            continue
        try:
            txt = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if len(txt) >= 6 and _printable_share(txt) >= 0.92 and any(c.isalpha() for c in txt):
            return txt
    return None


def _base64_view(text: str) -> str | None:
    """The text with every long base64 run replaced by what it decodes to (when that reads like text)."""
    if len(text) < 16:
        return None
    budget = [MAX_DECODED_BYTES]
    count = [0]

    def rep(m: re.Match) -> str:
        count[0] += 1
        if count[0] > 50_000 or budget[0] <= 0:
            return m[0]
        dec = _try_base64(m[0])
        if dec is None:
            return m[0]
        budget[0] -= len(dec)
        return " " + _san(dec) + " "

    out = _RX_B64.sub(rep, text)
    return out if out != text else None


def _views(text: str) -> list[View]:
    views: list[View] = [(text, None)]
    seen = {text}

    def add(t: str, lm: list[int] | None) -> None:
        if t not in seen and t.strip():
            seen.add(t)
            views.append((t, lm))

    if _CONTROLS.search(text):                       # fold drops a control inside a word; between two words it separates
        add(_CONTROLS.sub(" ", text), None)

    base, base_map = text, None
    joined = _qp_join(text)
    if joined is not None:
        base, base_map = joined
        add(base, base_map)
    dec = _unescape(base)
    if dec != base:
        add(dec, base_map)
    for src in (base, dec):
        b64 = _base64_view(src)
        if b64 is not None:
            b64b = _base64_view(b64)                 # base64 inside base64, once
            for t in (b64, _unescape(b64), b64b, _unescape(b64b) if b64b else None):
                if t:
                    add(t, base_map)
    return views


def _map_line(lm: list[int] | None, line: int) -> int:
    if lm is None:
        return line
    return lm[line - 1] if 0 < line <= len(lm) else line


# ---------------------------------------------------------------- the owner's terms

class TermError(Exception):
    """A rule in the list that cannot be used. Names the rule by number; never quotes it."""

    def __init__(self, index: int, list_line: int, problem: str):
        super().__init__(f"personal term #{index} (list line {list_line}): {problem}")
        self.index = index
        self.list_line = list_line


class TermsFileError(Exception):
    """A list that was named on purpose cannot be used."""


@dataclass(frozen=True)
class Rule:
    index: int          # 1-based among the rules (comments and empty lines do not count)
    list_line: int      # 1-based line in the list
    pattern: re.Pattern
    is_regex: bool


_WORD = re.compile(r"[^\W_]+")
_SEP = r"(?:\\[nrt]|[\W_])*"
_UMLAUT_BASE = {"\u00e4": "a", "\u00f6": "o", "\u00fc": "u"}


def _left_edge_ok(text: str, m: re.Match) -> bool:
    """A term that starts with a letter is not found inside a longer word; one that starts with a digit is not found
    inside a longer number. (Checked here, not in the pattern: a pattern that starts with a literal is searched
    much faster.)"""
    start = m.start()
    if start == 0:
        return True
    first, before = text[start], text[start - 1]
    if first.isalpha():
        return not before.isalpha()
    if first.isdigit():
        return not before.isdigit()                      # (a letter glued to a number is fine: "ID0795551234", "12a")
    return True


def _edge_matches(pattern: re.Pattern, text: str) -> Iterator[re.Match]:
    pos, n = 0, len(text)
    while pos <= n:
        m = pattern.search(text, pos)
        if m is None:
            return
        if _left_edge_ok(text, m):
            yield m
            pos = max(m.end(), m.start() + 1)
        else:
            pos = m.start() + 1


def _edge_after(ch: str) -> str:
    if ch.isalpha():
        return r"(?![^\W\d_])"
    if ch.isdigit():
        return r"(?!\d)"
    return ""


def _word_regex(word: str) -> str:
    """One word of a term, in the folded alphabet. a, o, u (and the umlauts) may carry an e after them: the umlaut as
    a plain vowel (Muller), as the two-letter form (Mueller) or as the letter itself (folded to the two-letter form)."""
    out: list[str] = []
    after_vowel = False
    for ch in word:
        if ch in _UMLAUT_BASE:
            out.append(_UMLAUT_BASE[ch] + "(?:e)?")
            after_vowel = True
            continue
        if ch == "e" and after_vowel:                    # already covered by the optional e
            after_vowel = False
            continue
        after_vowel = ch in "aou"
        out.append(re.escape(fold(ch)) + ("(?:e)?" if after_vowel else ""))
    return "".join(out)


def _term_regex(raw: str) -> re.Pattern | None:
    cleaned = _drop_invisible(unicodedata.normalize("NFC", raw).casefold())
    words = _WORD.findall(cleaned)
    if not words:
        return None
    orders = [words]
    if 2 <= len(words) <= 3 and all(w.isalpha() for w in words):
        orders.append([words[-1], *words[:-1]])           # "Last, First"
    alts: list[str] = []
    for order in orders:
        body = _SEP.join(_word_regex(w) for w in order)
        alts.append(body + _edge_after(order[-1][-1]))
    digits = "".join(words)
    if digits.isdigit() and len(digits) >= 9 and words[0].startswith("0"):
        # a national number (0 ...) is also written with the country code (+41 ..., 0041 ...)
        rest = _SEP.join(p for p in [re.escape(words[0][1:]), *map(re.escape, words[1:])] if p)
        alts.append(r"(?:0|(?:\+|00)\d{1,3}[\W_]*0?)" + rest + _edge_after(words[-1][-1]))
    pattern = alts[0] if len(alts) == 1 else "(?:" + "|".join(alts) + ")"
    return re.compile(pattern)


_LITERAL_NEWLINE = re.compile(r"(?:\\r)?\\n")


class TermList:
    def __init__(self, rules: list[Rule], notes: list[str] | None = None):
        self.rules = rules
        self.notes = notes or []
        self._plain = [r for r in rules if not r.is_regex]
        self._regexes = [r for r in rules if r.is_regex]
        self._any = re.compile("|".join(f"(?:{r.pattern.pattern})" for r in self._plain)) if self._plain else None

    @classmethod
    def parse(cls, text: str) -> "TermList":
        rules: list[Rule] = []
        notes: list[str] = []
        split_lines = 0
        for list_line, raw in enumerate(text.lstrip("\ufeff").split("\n"), 1):
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            is_regex = s[:6].lower() == "regex:"
            pieces = [s] if is_regex else [p.strip() for p in _LITERAL_NEWLINE.split(s)]
            if len(pieces) > 1:
                split_lines += 1
            for piece in pieces:
                if not piece:
                    continue
                index = len(rules) + 1
                if "\ufffd" in piece or "\x00" in piece:
                    raise TermError(index, list_line, "the list is not valid UTF-8 text (save it as UTF-8)")
                if piece[:6].lower() == "regex:":
                    body = piece[6:].strip()
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
                    continue
                if "," in piece or ";" in piece:
                    raise TermError(index, list_line, "contains a comma or semicolon. Put one term per line (a list "
                                    "on one line is read as a single term); start the line with regex: if the "
                                    "punctuation belongs to the term")
                term_rx = _term_regex(piece)
                if term_rx is None:
                    raise TermError(index, list_line, "no letters or digits in the term")
                rules.append(Rule(index, list_line, term_rx, False))
        if split_lines:
            notes.append(f"{split_lines} line(s) of the list held a literal \\n and were read as separate terms; "
                         "put one term per line")
        return cls(rules, notes)

    def describe(self, index: int) -> str:
        return f"personal term #{index} (list line {self.rules[index - 1].list_line})"

    def find_spans(self, text: str, views: list[View] | None = None) -> list[tuple[int, int, int]]:
        """(first line, last line, rule index) for every hit, sorted. A term that is a word is not found inside a
        longer word. The text is also looked at decoded (see _views)."""
        if not self.rules or not text:
            return []
        hits: set[tuple[int, int, int]] = set()
        for vtext, lm in views if views is not None else _views(text):
            folded = fold(vtext)
            if self._any is not None and self._any.search(folded):
                for r in self._plain:
                    for m in itertools.islice(_edge_matches(r.pattern, folded), MAX_HITS_PER_RULE):
                        a = _line(folded, m.start())
                        b = a + folded.count("\n", m.start(), m.end())
                        hits.add((_map_line(lm, a), _map_line(lm, b), r.index))
            if self._regexes:
                written = unicodedata.normalize("NFC", vtext)
                for r in self._regexes:
                    for src in (written, folded):
                        for m in itertools.islice(r.pattern.finditer(src), MAX_HITS_PER_RULE):
                            a = _line(src, m.start())
                            b = a + src.count("\n", m.start(), m.end())
                            hits.add((_map_line(lm, a), _map_line(lm, b), r.index))
        return sorted(hits)

    def find(self, text: str) -> list[tuple[int, int]]:
        """(line, rule index) for every hit, sorted."""
        return sorted({(a, idx) for a, _b, idx in self.find_spans(text)})


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


def decode_terms_file(data: bytes) -> tuple[str, str | None]:
    """The text of a list file and a note if it was not UTF-8. Windows PowerShell writes UTF-16 (> and Out-File) or
    Windows-1252 (Set-Content, Add-Content); both are read instead of silently matching nothing."""
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return data.decode("utf-32", "replace"), "the list is UTF-32; save it as UTF-8"
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", "replace"), "the list is UTF-16; save it as UTF-8"
    wide = _wide_codec(data)
    if wide:
        return data.decode(wide, "replace"), f"the list is {wide[:6].upper()} without a BOM; save it as UTF-8"
    try:
        return data.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace"), "the list is not UTF-8; it was read as Windows-1252. Save it as UTF-8"


def load_terms(env: Mapping[str, str], home: Path | None, windows: bool, cli_file: str | None = None) -> TermsSource:
    """The first list that exists and has rules. Raises TermError for a rule that cannot be used and TermsFileError
    for a list that was named on purpose and cannot be used."""
    warnings: list[str] = []
    candidates: list[tuple[str, Path, bool]] = []          # label, path, named on purpose
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
                raise TermsFileError(f"{label} names a personal-terms list that does not exist (a typo would switch "
                                     "the check off, so this is an error)")
            continue
        text, note = decode_terms_file(path.read_bytes())
        if note:
            warnings.append(f"{label}: {note}")
        terms = TermList.parse(text)
        warnings += [f"{label}: {n}" for n in terms.notes]
        if not terms.rules:
            if named:
                raise TermsFileError(f"{label} names a personal-terms list that has no terms")
            warnings.append(f"{label}: the list has no terms; looking elsewhere")
            continue
        return TermsSource(terms, f"{len(terms.rules)} rules from {label} ({path})", warnings)
    text = env.get("KAIRO_PERSONAL_TERMS", "")
    if text.strip():
        terms = TermList.parse(text)
        warnings += [f"KAIRO_PERSONAL_TERMS: {n}" for n in terms.notes]
        if terms.rules:
            return TermsSource(terms, f"{len(terms.rules)} rules from env KAIRO_PERSONAL_TERMS", warnings)
        warnings.append("KAIRO_PERSONAL_TERMS has no terms")
    return TermsSource(None, "", warnings)


# ---------------------------------------------------------------- forbidden paths

_ENV_EXAMPLES = {".env.example", ".env.sample", ".env.template", ".env.dist"}
_SSH_NAMES = {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
_KEYSTORE_EXT = (".p12", ".pfx", ".jks", ".keystore")
_DATABASE_PATTERNS = ("*.sqlite*", "*.db", "*.db3", "*.db-wal", "*.db-shm", "*.db-journal", "*.s3db", "*.sdb")
_FORBIDDEN_DIRS = ("private", "data", "devdata", "secrets", "vault", ".private")


def forbidden_path(path: str) -> list[str]:
    """Why a path must never be committed or shipped (names of rules, empty if it is fine)."""
    p = path.replace("\\", "/")
    parts = [x.rstrip(" .") if x not in ("..",) else x for x in p.split("/") if x not in ("", ".")]
    parts = [x for x in parts if x]
    if not parts:
        return []
    is_dir = p.endswith("/")
    dirs = parts if is_dir else parts[:-1]
    name = "" if is_dir else parts[-1].lower()
    out: list[str] = []
    for d in dirs:
        dl = d.lower()
        for folder in _FORBIDDEN_DIRS:
            if dl == folder and f"{folder}/ folder" not in out:
                out.append(f"{folder}/ folder")
    if name:
        if fnmatch.fnmatchcase(name, "*.local.*"):
            out.append("*.local.* file")
        if name not in _ENV_EXAMPLES and (name == ".env" or name == ".envrc" or name.startswith((".env.", ".env-"))
                                          or name.endswith(".env")):
            out.append(".env file")
        if any(fnmatch.fnmatchcase(name, pat) for pat in _DATABASE_PATTERNS):
            out.append("database file")
        if name.startswith("personal-terms"):
            out.append("personal terms list")
        if fnmatch.fnmatchcase(name, "client_secret*.json"):
            out.append("Google client secret file")
        if name in _SSH_NAMES:
            out.append("ssh private key file")
        if name.endswith(_KEYSTORE_EXT):
            out.append("key store file")
        if name.endswith(".key"):
            out.append("key file")
        if name.startswith("capture-token"):
            out.append("capture token file")
        if name == ".gitleaksignore":
            out.append("gitleaks ignore file (it would silence gitleaks in CI)")
        if PurePosixPath(name).suffix in DOCUMENT_EXT:
            norm = "/" + "/".join(parts).lower()
            if not any("/" + d in norm for d in DOCUMENT_ALLOWED_DIRS):
                out.append("document or photo file (only web/public/ may hold them)")
    return out


_MAGIC_FORBIDDEN = (
    (b"SQLite format 3\x00", "SQLite database (by its header)"),
    (b"KAIRO-DEV1\n", "Kairo secret store (by its header)"),
    (b"KAIRO-DPAPI1\n", "Kairo secret store (by its header)"),
)


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
_BENIGN_PART = re.compile(r"^(?:hash\w*|sha\d*|md5|digest|checksum|fingerprint|public\w*|pubkey|pub)$")
_SECRET_WORDS = re.compile(r"password|passwd|passphrase|secret|token|apikey|privatekey|authkey|accesskey|credential|vapidprivate|"
                           r"session|cookie")


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


def _identifier_like(value: str) -> bool:
    """loops.cleanup.2026-10-19, kairo:calendar:last_sync_token_2026: words joined by punctuation, not a generated key."""
    segments = [seg for seg in re.split(r"[^A-Za-z0-9]+", value) if seg and not seg.isdigit()]   # dates/versions are neutral
    words = [seg for seg in segments if seg.isalpha() and len(seg) >= 3]
    return len(words) >= 2 and len(words) * 2 >= len(segments)


def _random_value(value: str) -> bool:
    """A long value that looks like a generated key, not a word, a name, a path or a placeholder."""
    if len(value) < 20 or _placeholder(value) or _UUID.match(value):
        return False
    if _identifier_like(value):
        return False
    if value[:1] in "/~." or "://" in value or _FILE_VALUE.search(value):
        return False
    digits = sum(c.isdigit() for c in value)
    return digits >= 2 and any(c.isalpha() for c in value) and _entropy(value) >= 3.2


def _fake_body(body: str, whole: str) -> bool:
    return whole in ALLOWED_FAKE_VALUES or whole.lower().startswith(tuple(p.lower() for p in ALLOWED_FAKE_PREFIXES)) \
        or _trivial(body)


def _name_parts(name: str) -> list[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return [p.lower() for p in re.split(r"[_\-.]+", spaced) if p]


def _secret_name(name: str) -> bool:
    """Does a variable or key name say that its value is a secret? (password, token, ..., and names ending in key)"""
    parts = _name_parts(name)
    if not parts or any(_BENIGN_PART.match(p) for p in parts):
        return False
    if _SECRET_WORDS.search("".join(parts)):
        return True
    return parts[-1] == "key" and len(parts) >= 2


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
        r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----(?:(?!-----END)[\s\S]){0,4000}?[A-Za-z0-9+/]{24,}"), _always,
        ("PRIVATE KEY",)),
    SecretRule("private key", re.compile(                 # the same, wrapped at 64 or 16 characters: lines of base64 right under the header
        r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----[ \t]*\r?\n(?:[A-Za-z0-9+/=]{8,}[ \t]*\r?\n){2,}"), _always,
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
    SecretRule("password in a URL", re.compile(r"://[^\s:/@\"']{1,64}:(?P<body>[^\s@/\"'<>]{12,})@[A-Za-z0-9]"),
               lambda m: _random_value(m["body"]), ("://",)),
    SecretRule("healthchecks ping URL", re.compile(
        r"hc-ping\.com/(?P<body>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"),
               lambda m: not _trivial(m["body"]), ("hc-ping.com",)),
    SecretRule("private JWK", re.compile(r"[\"']d[\"']\s*:\s*[\"'](?P<body>[A-Za-z0-9_\-]{43,})[\"']"),
               lambda m: ("kty" in m.string) and not _trivial(m["body"]) and not _placeholder(m["body"]), ("kty",)),
    SecretRule("VAPID private key", re.compile(
        r"vapid[\s\S]{0,100}?private[\"']?\s*[:=]\s*[\"']?(?P<body>[A-Za-z0-9_\-+/=]{40,})", re.I),
               lambda m: not _trivial(m["body"]) and not _placeholder(m["body"]), ("vapid",), True),
]

# gitleaks skips every line that holds its allow comment, whatever config CI uses (built from pieces so that this file
# does not hold it)
_GITLEAKS_ALLOW = re.compile("gitleaks" + ":allow", re.I)
GITLEAKS_ALLOW_NAME = "gitleaks allow comment (only server/tests, docs and installer/ci may hold one)"

_ASSIGN = re.compile(
    r"[\"'`]?[ \t]*(?::=|=>|[:=])[ \t]*[\"'`]?(?P<body>[A-Za-z0-9_\-+/=.~!@#$%^&*?:]{20,})(?![\w(\[{])")
_NAME_BEFORE = re.compile(r"([A-Za-z0-9_\-.]{1,80})$")
GENERIC_NAME = "password/secret/token assignment"
_GENERIC_NEEDLES = ("password", "passwd", "passphrase", "secret", "token", "key", "credential", "private", "session", "cookie")


def _secret_spans_one(text: str, pragma_ok: bool) -> list[tuple[int, int, str]]:
    found: dict[tuple[int, int], list[str]] = {}
    named_lines: set[int] = set()

    def line_text(pos: int) -> str:
        start = text.rfind("\n", 0, pos) + 1
        end = text.find("\n", pos)
        return text[start: len(text) if end < 0 else end]

    def add(start: int, end: int, name: str) -> None:
        if pragma_ok and _PRAGMA.search(line_text(start)):
            return
        a = _line(text, start)
        b = a + text.count("\n", start, max(start, end - 1))
        names = found.setdefault((a, b), [])
        if name not in names:
            names.append(name)
        named_lines.add(a)

    low = text.lower()
    for rule in _RULES:
        hay = low if rule.lower else text
        if rule.needles and not any(n in hay for n in rule.needles):
            continue
        for m in rule.rx.finditer(text):
            if rule.ok(m):
                add(m.start(), m.end(), rule.name)
    if not pragma_ok and "gitleaks" in low:
        for m in _GITLEAKS_ALLOW.finditer(text):
            add(m.start(), m.end(), GITLEAKS_ALLOW_NAME)
    if any(k in low for k in _GENERIC_NEEDLES):
        for m in _ASSIGN.finditer(text):
            if not _random_value(m["body"]):
                continue
            nm = _NAME_BEFORE.search(text[max(0, m.start() - 100): m.start()])
            if nm and _secret_name(nm[1]) and _line(text, m.start()) not in named_lines:     # a specific rule may have named it
                add(m.start(), m.end(), GENERIC_NAME)
    return sorted((a, b, name) for (a, b), names in found.items() for name in names)


def secret_spans(text: str, pragma_ok: bool = True, views: list[View] | None = None) -> list[tuple[int, int, str]]:
    """(first line, last line, rule name) for every secret in the text, sorted. Never returns the value."""
    out: set[tuple[int, int, str]] = set()
    for vtext, lm in views if views is not None else _views(text):
        for a, b, name in _secret_spans_one(vtext, pragma_ok):
            out.add((_map_line(lm, a), _map_line(lm, b), name))
    return sorted(out)


def scan_secrets(text: str) -> list[tuple[int, str]]:
    """(line, rule name) for every secret in the text, sorted. Never returns the value."""
    return sorted({(a, name) for a, _b, name in secret_spans(text)})


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
        self.unscanned: list[str] = []                     # could not be read: an error in strict mode
        self.errors: list[str] = []
        self.items = 0

    def add(self, f: Finding) -> None:
        self.findings.setdefault(f)


class GitError(Exception):
    pass


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    # GIT_NO_REPLACE_OBJECTS: what is pushed are the real objects, not what a local refs/replace/ ref shows in their place
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_PAGER="cat", GIT_NO_REPLACE_OBJECTS="1")
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


# ---------------------------------------------------------------- reading bytes as text

def _wide_codec(data: bytes) -> str | None:
    """utf-32-le / -be or utf-16-le / -be for a text without a BOM (the high bytes of each unit are NUL), else None."""
    sample = data[:4096]
    quarter = len(sample) // 4
    if quarter >= 4:
        cols = [sample[k: quarter * 4: 4].count(0) for k in range(4)]
        if cols[2] > 0.9 * quarter and cols[3] > 0.9 * quarter and cols[0] < 0.05 * quarter:
            return "utf-32-le"
        if cols[0] > 0.9 * quarter and cols[1] > 0.9 * quarter and cols[3] < 0.05 * quarter:
            return "utf-32-be"
    half = len(sample) // 2
    if half < 4:
        return None
    even_nul = sample[0::2].count(0)
    odd_nul = sample[1::2].count(0)
    if odd_nul > 0.3 * half and even_nul < 0.05 * half:
        return "utf-16-le"
    if even_nul > 0.3 * half and odd_nul < 0.05 * half:
        return "utf-16-be"
    return None


# A readable string in a binary: printable ASCII, well-formed UTF-8, or a single latin-1 letter between two letters
# (Mueller written with an umlaut in an old Windows file). Random bytes hardly ever form such a run.
_STR_UNIT = (rb"(?:[\x09\x20-\x7e]|[\xc2-\xdf][\x80-\xbf]|[\xe0-\xef][\x80-\xbf]{2}|[\xf0-\xf4][\x80-\xbf]{3}"
             rb"|(?<=[A-Za-z])[\xa0-\xff](?=[A-Za-z]))")
_STR_RUN = re.compile(_STR_UNIT + rb"{4,}")
_STR16LE = re.compile(rb"(?:[\x09\x20-\x7e\xa0-\xff]\x00){4,}")
_STR16BE = re.compile(rb"(?:\x00[\x09\x20-\x7e\xa0-\xff]){4,}")


def _binary_texts(data: bytes) -> list[str]:
    """The readable strings of a binary: 8-bit runs (as UTF-8 and as latin-1) and UTF-16 runs."""
    out: list[str] = []
    joined = b"\n".join(_STR_RUN.findall(data))
    if joined:
        out.append(joined.decode("utf-8", "replace"))
        if not joined.isascii():
            out.append(joined.decode("latin-1"))
    le = [m.decode("utf-16-le", "replace") for m in _STR16LE.findall(data)]
    if le:
        out.append("\n".join(le))
    be = [m.decode("utf-16-be", "replace") for m in _STR16BE.findall(data)]
    if be:
        out.append("\n".join(be))
    return out


def _inflate(chunk: memoryview | bytes, limit: int = 32 * 1024 * 1024) -> bytes | None:
    try:
        return zlib.decompressobj().decompress(chunk, limit)
    except zlib.error:
        return None


def _png_meta(data: bytes) -> bytes:
    """A PNG without its pixel data (IDAT chunks): the part that can hold text."""
    pos, out = 8, []
    while pos + 12 <= len(data):
        length = int.from_bytes(data[pos: pos + 4], "big")
        typ = data[pos + 4: pos + 8]
        end = pos + 8 + length
        if end > len(data):
            return data                                  # not a well-formed PNG: read all of it
        if typ != b"IDAT":
            out.append(data[pos + 4: end])
        pos = end + 4
        if typ == b"IEND":
            out.append(data[pos:])                       # whatever was appended after the image
            return b"\x00".join(out)
    return data


def _png_texts(data: bytes) -> list[str]:
    """Compressed text chunks (zTXt, iTXt) of a PNG; plain tEXt chunks are found as binary strings."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return []
    out: list[str] = []
    pos = 8
    while pos + 8 <= len(data):
        length, typ = struct.unpack(">I4s", data[pos: pos + 8])
        body = data[pos + 8: pos + 8 + length]
        pos += 12 + length
        if typ == b"zTXt":
            kw, _, rest = body.partition(b"\x00")
            raw = _inflate(rest[1:])
            if raw:
                out.append(kw.decode("latin-1") + "\n" + raw.decode("utf-8", "replace"))
        elif typ == b"iTXt":
            kw, _, rest = body.partition(b"\x00")
            if len(rest) >= 2:
                flag, rest = rest[0], rest[2:]
                _lang, _, rest = rest.partition(b"\x00")
                _trans, _, text = rest.partition(b"\x00")
                raw = _inflate(text) if flag else text
                if raw:
                    out.append(kw.decode("latin-1") + "\n" + raw.decode("utf-8", "replace"))
        elif typ == b"IEND":
            break
    return out


_PDF_STREAM = re.compile(rb"stream\r?\n")
_PDF_TJ = re.compile(rb"\[((?:[^\]\\]|\\.)*)\]\s*TJ", re.S)
_PDF_TJ_ITEM = re.compile(rb"\(((?:[^()\\]|\\.|\((?:[^()\\]|\\.)*\))*)\)|<([0-9A-Fa-f\s]*)>|(-?\d+(?:\.\d+)?)", re.S)
_PDF_TJ_SINGLE = re.compile(rb"\(((?:[^()\\]|\\.|\((?:[^()\\]|\\.)*\))*)\)\s*(?:Tj|'|\")", re.S)
_PDF_ESC = re.compile(rb"\\([0-7]{1,3}|\r\n|[\s\S])")
_PDF_SIMPLE = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}


def _pdf_string(raw: bytes) -> str:
    def rep(m: re.Match) -> bytes:
        g = m[1]
        if g[:1].isdigit():
            return bytes([int(g, 8) & 0xFF])
        if g in (b"\r\n", b"\n", b"\r"):
            return b""
        return _PDF_SIMPLE.get(g, g)

    b = _PDF_ESC.sub(rep, raw)
    return b.decode("utf-16-be", "replace") if b[:2] == b"\xfe\xff" else b.decode("latin-1")


def _pdf_show_text(chunk: bytes) -> str:
    """The text a PDF content stream shows: the pieces of a TJ array are joined (kerning splits words into pieces)."""
    out: list[str] = []
    for m in _PDF_TJ.finditer(chunk):
        piece: list[str] = []
        for it in _PDF_TJ_ITEM.finditer(m[1]):
            if it[1] is not None:
                piece.append(_pdf_string(it[1]))
            elif it[2] is not None:
                hx = re.sub(rb"\s+", b"", it[2])
                if len(hx) % 2 == 0 and hx:
                    b = bytes.fromhex(hx.decode())
                    piece.append(b.decode("utf-16-be", "replace") if b[:2] == b"\xfe\xff" else b.decode("latin-1"))
            elif it[3] is not None and float(it[3]) <= -200:
                piece.append(" ")
        out.append("".join(piece))
    for m in _PDF_TJ_SINGLE.finditer(chunk):
        out.append(_pdf_string(m[1]))
    return "\n".join(out)


def _pdf_texts(data: bytes) -> list[str]:
    """What a PDF holds that is not plain text in the file: its Flate streams (content, metadata, object streams)."""
    texts: list[str] = []
    total = 0
    mv = memoryview(data)
    for m in _PDF_STREAM.finditer(data):
        chunk = _inflate(mv[m.end(): m.end() + 32 * 1024 * 1024])
        if not chunk:
            continue
        total += len(chunk)
        texts.extend(_binary_texts(chunk))
        shown = _pdf_show_text(chunk)
        if shown.strip():
            texts.append(shown)
        if total > MAX_DECODED_BYTES * 4:
            break
    return texts


def _text_forms(data: bytes) -> list[tuple[str, bool]]:
    """(text, binary) for every way a file's bytes can be read as text."""
    forms: list[tuple[str, bool]] = []
    if data[:4] in (codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE):
        forms.append((data.decode("utf-32", "replace"), False))
    elif data[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
        forms.append((data.decode("utf-16", "replace"), False))
    elif (wide := _wide_codec(data)) is not None:
        forms.append((data.decode(wide, "replace"), False))
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        forms.extend((t, True) for t in _binary_texts(_png_meta(data)))
    elif b"\x00" in data[:8192]:
        forms.extend((t, True) for t in _binary_texts(data))
    else:
        try:
            forms.append((data.decode("utf-8-sig"), False))
        except UnicodeDecodeError:
            forms.append((data.decode("utf-8", "replace"), False))
            forms.append((data.decode("latin-1"), False))      # old Windows files: the umlauts are single bytes
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        forms.extend((t, True) for t in _png_texts(data))
    if b"%PDF-" in data[:1024]:
        forms.extend((t, True) for t in _pdf_texts(data))
    return forms


# ---------------------------------------------------------------- archives

_ARCHIVE_SUFFIX = {"gz": (".gz", ".gzip"), "bz2": (".bz2",), "xz": (".xz", ".lzma")}


def _archive_kind(data: bytes) -> str | None:
    head = data[:8]
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "zip"
    if head[:2] == b"\x1f\x8b":
        return "gz"
    if head[:3] == b"BZh" and len(data) > 4 and data[3:4].isdigit():
        return "bz2"
    if head[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if data[257:262] == b"ustar":
        return "tar"
    if head[:4] == b"\x28\xb5\x2f\xfd":
        return "zstd"
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if head[:6] == b"Rar!\x1a\x07":
        return "rar"
    if head[:4] == b"MSCF":
        return "cab"
    if head[:8] == b"!<arch>\n":
        return "ar"
    if head[:2] == b"\x1f\x9d":
        return "compress"
    if len(data) > 22:                                          # a zip with data in front (an installer stub, junk)
        tail = data.rfind(b"PK\x05\x06", max(0, len(data) - 70_000))
        if tail >= 0 and data.find(b"PK\x03\x04") >= 0:
            return "zip"
    return None


class _TooBig(Exception):
    pass


def _other_zip_names(info: zipfile.ZipInfo) -> list[str]:
    """A name without the UTF-8 flag is read by zipfile as DOS (cp437). Old Windows tools write Windows-1252 (or latin-1):
    the same bytes, read that way, are the other candidates for the name."""
    if info.flag_bits & 0x800:
        return []
    try:
        raw = info.orig_filename.encode("cp437")
    except UnicodeEncodeError:
        return []
    out = []
    for enc in ("cp1252", "latin-1"):
        try:
            alt = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if alt != info.filename and alt not in out:
            out.append(alt)
    return out


def _decompress(kind: str, data: bytes) -> bytes:
    limit = MAX_BYTES
    if kind == "gz":
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:
            out = f.read(limit + 1)
    elif kind == "bz2":
        out = bz2.BZ2Decompressor().decompress(data, limit + 1)
    else:
        out = lzma.LZMADecompressor().decompress(data, max_length=limit + 1)
    if len(out) > limit:
        raise _TooBig
    return out


def _walk_zip_local_headers(data: bytes) -> Iterator[tuple[str, bytes | None, str]]:
    """Members of a zip that zipfile will not open, read from the local headers: (name, content or None, why not)."""
    pos = 0
    while True:
        i = data.find(b"PK\x03\x04", pos)
        if i < 0 or i + 30 > len(data):
            return
        _sig, _ver, flags, method, _t, _d, _crc, csize, _usize, nlen, elen = struct.unpack("<4sHHHHHIIIHH", data[i: i + 30])
        raw_name = data[i + 30: i + 30 + nlen]
        name = raw_name.decode("utf-8" if flags & 0x800 else "cp437", "replace")
        start = i + 30 + nlen + elen
        pos = start
        if flags & 1:
            yield name, None, "encrypted"
            continue
        if method == 0 and not (flags & 8 and csize == 0):
            yield name, data[start: start + csize], ""
            pos = start + csize
        elif method == 8:
            d = zlib.decompressobj(-15)
            try:
                content = d.decompress(memoryview(data)[start:], MAX_BYTES)
            except zlib.error:
                yield name, None, "damaged"
                continue
            yield name, content, ""
            pos = len(data) - len(d.unused_data)
        else:
            yield name, None, f"compression method {method}"


# ---------------------------------------------------------------- the scanner

class Scanner:
    def __init__(self, terms: TermList | None, secrets: bool = True) -> None:
        self.terms = terms
        self.secrets = secrets
        self.report = Report()
        self._safe_cache: dict[str, str] = {}
        self._names: dict[str, int] = {}
        self._archive_budget = MAX_ARCHIVE_TOTAL

    # a name that holds a term is never printed (numbered per run: a hash of a guessable name could be checked offline)
    def safe(self, raw: str) -> str:
        shown = self._safe_cache.get(raw)
        if shown is None:
            shown = raw
            if self.terms and self.terms.find(raw):
                shown = f"<redacted name #{self._names.setdefault(raw, len(self._names) + 1)}>"
            self._safe_cache[raw] = shown
        return shown

    def scrub(self, message: str) -> str:
        """A message of the tool or of git, safe to print: gone if it holds a personal term."""
        if self.terms and self.terms.find(message):
            return "<message hidden: it contains a personal term>"
        return message

    def add(self, where: str, line: int | None, rule: str, extra: str = "") -> None:
        self.report.add(Finding(where, line, rule, extra))

    def unscanned(self, path: str, why: str) -> None:
        self.report.unscanned.append(f"{self.safe(path)}: not scanned, {why}")

    def check_path(self, path: str, prefix: str = "", extra: str = "") -> None:
        where = prefix + self.safe(path)
        for name in forbidden_path(path):
            self.add(where, None, f"forbidden path: {name}", extra)
        if self.terms:
            for _line_no, idx in self.terms.find(path):
                self.add(where, None, self.terms.describe(idx) + " in the name", extra)

    def scan_text(self, where: str, text: str, only_lines: set[int] | None = None, extra: str = "",
                  one_line: bool = False, base_line: int = 0, pragma_ok: bool = False) -> None:
        """Report every hit in a text. With only_lines, a hit counts if any line of it (a key block, a name over two
        lines) is one of those lines."""
        views = _views(text)
        hits: list[tuple[int, int, str]] = []
        if self.terms:
            hits += [(a, b, self.terms.describe(idx)) for a, b, idx in self.terms.find_spans(text, views)]
        if self.secrets:
            hits += [(a, b, f"secret: {name}") for a, b, name in secret_spans(text, pragma_ok, views)]
        if only_lines is not None:
            wanted = sorted(only_lines)
            kept = []
            for a, b, rule in hits:
                i = bisect.bisect_left(wanted, a + base_line)
                if i < len(wanted) and wanted[i] <= b + base_line:
                    kept.append((a, b, rule))
            hits = kept
        hits.sort()
        for a, _b, rule in hits[:MAX_FINDINGS_PER_FILE]:
            self.add(where, None if one_line else a + base_line, rule, extra)
        if len(hits) > MAX_FINDINGS_PER_FILE:
            self.add(where, None, f"{len(hits) - MAX_FINDINGS_PER_FILE} more finding(s) in this file", extra)

    def _scan_content(self, path: str, where: str, data: bytes, only_lines: set[int] | None, extra: str) -> None:
        norm = "/" + path.replace("\\", "/").lstrip("/")
        pragma_ok = any("/" + d in norm for d in PRAGMA_DIRS)
        if len(data) > CHUNK_BYTES and _wide_codec(data) is None and data[:2] not in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE) \
                and b"\x00" not in data[:8192]:
            self._scan_in_chunks(where, data, only_lines, extra, pragma_ok)
            return
        for text, binary in _text_forms(data):
            self.scan_text(where, text, None if binary else only_lines, extra, one_line=binary, pragma_ok=pragma_ok)

    def _scan_in_chunks(self, where: str, data: bytes, only_lines: set[int] | None, extra: str, pragma_ok: bool) -> None:
        pos, n = 0, len(data)
        while pos < n:
            end = min(n, pos + CHUNK_BYTES)
            if end < n:
                cut = data.rfind(b"\n", pos + CHUNK_BYTES // 2, end)
                end = cut + 1 if cut >= 0 else end
            begin = max(0, pos - CHUNK_OVERLAP)
            piece = data[begin:end]
            base = data.count(b"\n", 0, begin)
            try:
                texts = [piece.decode("utf-8-sig" if begin == 0 else "utf-8")]
            except UnicodeDecodeError:
                texts = [piece.decode("utf-8", "replace"), piece.decode("latin-1")]
            for text in texts:
                self.scan_text(where, text, only_lines, extra, base_line=base, pragma_ok=pragma_ok)
            pos = end

    def scan_blob(self, path: str, data: bytes, *, only_lines: set[int] | None = None, extra: str = "",
                  depth: int = 0, path_checked: bool = False) -> None:
        self.report.items += 1
        if not path_checked:
            self.check_path(path, extra=extra)
        where = self.safe(path)
        if len(data) > MAX_BYTES:
            self.unscanned(path, f"{len(data) // 2 ** 20} MB is over the {MAX_BYTES // 2 ** 20} MB limit; only its name was checked")
            return
        for magic, what in _MAGIC_FORBIDDEN:
            if data.startswith(magic):
                self.add(where, None, f"forbidden content: {what}", extra)
        kind = _archive_kind(data)
        if kind is not None and self._scan_archive(path, where, data, kind, extra, depth):
            return
        self._scan_content(path, where, data, only_lines, extra)

    # ------------------------------------------------------------ archives

    def _take(self, path: str, size: int) -> bool:
        if size > MAX_BYTES or size > self._archive_budget:
            self.unscanned(path, f"{size // 2 ** 20} MB is over the limit; only its name was checked")
            return False
        return True

    def _scan_archive(self, path: str, where: str, data: bytes, kind: str, extra: str, depth: int) -> bool:
        """True if the archive's members were scanned (the raw bytes then need no scan of their own)."""
        if depth >= MAX_ARCHIVE_DEPTH:
            self.unscanned(path, f"archives are nested more than {MAX_ARCHIVE_DEPTH} deep")
            return False
        if kind == "zip":
            return self._scan_zip_bytes(path, where, data, extra, depth)
        if kind in ("gz", "bz2", "xz"):
            try:
                inner = _decompress(kind, data)
            except _TooBig:
                self.unscanned(path, "it unpacks to more than the size limit")
                return False
            except Exception as exc:                   # a damaged or truncated stream, bad data, not enough memory
                self.unscanned(path, f"could not be unpacked ({type(exc).__name__})")
                return False
            self._archive_budget -= len(inner)
            name = PurePosixPath(path.replace("\\", "/")).name
            low = name.lower()
            inner_name = "content"
            for suf in _ARCHIVE_SUFFIX[kind]:
                if low.endswith(suf) and len(name) > len(suf):
                    inner_name = name[: -len(suf)]
                    break
            if low.endswith(".tgz"):
                inner_name = name[:-4] + ".tar"
            self.scan_blob(f"{path}!/{inner_name}", inner, extra=extra, depth=depth + 1)
            self._scan_content(path, where, data[:2048], None, extra)           # the header (a stored file name ...)
            return True
        if kind == "tar":
            return self._scan_tar(path, where, data, extra, depth)
        self.unscanned(path, f"a {kind} archive cannot be opened here")
        return False

    def _scan_zip_bytes(self, path: str, where: str, data: bytes, extra: str, depth: int) -> bool:
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except Exception:                               # not a zip after all, or a damaged one, or odd names
            members = list(_walk_zip_local_headers(data))
            if not members:
                self.unscanned(path, "it looks like a zip but cannot be opened")
                return False
            self.report.warnings.append(f"{where}: damaged zip, read from its local headers")
            for name, content, why in members:
                full = f"{path}!/{name}"
                self.check_path(full, extra=extra)
                if content is None:
                    self.unscanned(full, why)
                elif self._take(full, len(content)):
                    self._archive_budget -= len(content)
                    self.scan_blob(full, content, extra=extra, depth=depth + 1, path_checked=True)
            return True
        with zf:
            first = data.find(b"PK\x03\x04")
            if first > 0:                               # an installer stub or other data in front of the zip
                self._scan_content(path, where, data[:first], None, extra)
            self.scan_zip(zf, path + "!/", extra=extra, depth=depth + 1)
        return True

    def scan_zip(self, zf: zipfile.ZipFile, prefix: str = "", extra: str = "", depth: int = 0) -> None:
        if zf.comment:
            self.report.items += 1
            self._scan_content(prefix + "(zip comment)", self.safe(prefix) + "(zip comment)", zf.comment, None, extra)
        infos = zf.infolist()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            self.unscanned(prefix or "archive", f"{len(infos)} entries, only the first {MAX_ARCHIVE_MEMBERS} were checked")
            infos = infos[:MAX_ARCHIVE_MEMBERS]
        for info in infos:
            full = prefix + info.filename
            self.check_path(full, extra=extra)
            for alt in _other_zip_names(info):                     # the same bytes read as Windows-1252 / latin-1
                if self.terms:
                    for _ln, idx in self.terms.find(prefix + alt):
                        self.add(self.safe(full), None, self.terms.describe(idx) + " in the name", extra)
            if info.comment:
                self._scan_content(full, self.safe(full) + "(entry comment)", info.comment, None, extra)
            if info.filename.endswith("/"):
                continue
            if info.flag_bits & 0x1:
                self.unscanned(full, "encrypted")
                continue
            if not self._take(full, info.file_size):
                continue
            try:
                data = zf.read(info)
            except Exception as exc:                    # encrypted, Deflate64, bad CRC, truncated, corrupt stream
                self.unscanned(full, f"could not be read ({type(exc).__name__})")
                continue
            self._archive_budget -= len(data)
            self.scan_blob(full, data, extra=extra, depth=depth, path_checked=True)

    def _scan_tar(self, path: str, where: str, data: bytes, extra: str, depth: int) -> bool:
        try:
            tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:")
        except Exception as exc:
            self.unscanned(path, f"could not be opened as a tar ({type(exc).__name__})")
            return False
        count = 0
        try:
            with tf:
                for member in tf:
                    count += 1
                    if count > MAX_ARCHIVE_MEMBERS:
                        self.unscanned(path, f"more than {MAX_ARCHIVE_MEMBERS} entries")
                        break
                    full = f"{path}!/{member.name}"
                    self.check_path(full, extra=extra)
                    if member.issym() or member.islnk():
                        self.scan_text(self.safe(full), member.linkname, extra=extra, one_line=True)
                        continue
                    if not member.isfile():
                        continue
                    if not self._take(full, member.size):
                        continue
                    handle = tf.extractfile(member)
                    content = handle.read() if handle is not None else b""
                    self._archive_budget -= len(content)
                    self.scan_blob(full, content, extra=extra, depth=depth + 1, path_checked=True)
        except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
            self.unscanned(path, f"could not be read to the end ({type(exc).__name__})")
        return True

    # ------------------------------------------------------------ modes

    def scan_zip_file(self, path: Path) -> None:
        self.report.items += 1
        self.check_path(path.name)
        try:
            zf = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError, ValueError) as exc:
            self.report.errors.append(f"{self.safe(path.name)}: not a readable zip ({type(exc).__name__})")
            return
        with zf:
            self.scan_zip(zf)

    def scan_tree(self, root: Path) -> None:
        for rel in _tree_files(root):
            full = root / rel
            try:
                if full.is_symlink():                  # the name and where it points are checked, not the target's file
                    self.report.items += 1
                    self.check_path(rel)
                    self.scan_text(self.safe(rel), os.readlink(full), one_line=True)
                    continue
                if not full.is_file():
                    continue
                size = full.stat().st_size
                if size > MAX_BYTES:
                    self.check_path(rel)
                    self.unscanned(rel, f"{size // 2 ** 20} MB is over the {MAX_BYTES // 2 ** 20} MB limit; only its name was checked")
                    continue
                data = full.read_bytes()
            except OSError as exc:
                self.check_path(rel)
                self.unscanned(rel, f"could not be read ({type(exc).__name__})")
                continue
            self.scan_blob(rel, data)

    def scan_staged(self, repo: Path) -> None:
        raw = _git(repo, "diff", "--cached", "--raw", "-z", "--no-abbrev", "--no-renames", "--no-textconv",
                   "--no-ext-diff", "--diff-filter=ACMRT").split(b"\0")
        entries: list[tuple[str, str, str]] = []                     # path, mode, blob sha
        i = 0
        while i + 1 < len(raw):
            meta, path_b = raw[i], raw[i + 1]
            i += 2
            fields = _dec(meta).lstrip(":").split(" ")
            if len(fields) >= 5:
                entries.append((_dec(path_b), fields[1], fields[3]))
        blobs: dict[str, bytes | None] = {}
        wanted = sorted({sha for _p, mode, sha in entries if mode != "160000"})
        for sha, typ, data in self._read_objects(repo, wanted):
            blobs[sha] = data
        for path, mode, sha in entries:
            self.check_path(path)
            if mode == "160000":                                     # a submodule bump has no file to read
                self.report.warnings.append(f"{self.safe(path)}: a submodule, no file content to scan")
                continue
            if sha not in blobs:
                self.report.errors.append(f"{self.safe(path)}: the staged file could not be read")
                continue
            data = blobs[sha]
            if data is None:
                self.unscanned(path, "it is over the size limit; only its name was checked")
                continue
            diff = _dec(_git(repo, "diff", "--cached", "-U0", "--no-color", "--no-ext-diff", "--no-textconv",
                             "--no-renames", "--", f":(literal){path}"))
            if re.search(r"^Binary files .* differ$", diff, re.M) or "GIT binary patch" in diff:
                lines = None                                         # binary: the whole file counts
            else:
                lines = _added_lines(diff)
                if not lines:
                    continue
            self.scan_blob(path, data, only_lines=lines, path_checked=True)

    def scan_message_file(self, path: Path, prepared: Path | None = None, repo: Path | str = ".") -> None:
        """A commit message. Every line is scanned: with -m, -F and `git merge -m` git keeps '#' lines and the text
        below a scissors line. Only with `prepared` (an editor was opened and the prepare-commit-msg hook saved the
        file as git handed it over) are the lines git itself appended left out, while they stand unchanged at the
        end and git's cleanup mode drops them."""
        texts = _message_texts(path.read_bytes())
        before: list[str | None] = [None]
        if prepared is not None and (_git_config(repo, "commit.cleanup") or "default").lower() in ("default", "strip", "scissors"):
            before = _message_texts(prepared.read_bytes())         # decoded the same way as the message
        marker = _git_config(repo, "core.commentString") or _git_config(repo, "core.commentChar") or "#"
        self.report.items += 1
        for i, text in enumerate(texts):
            self.scan_text("commit message", _message_body(text, before[min(i, len(before) - 1)], marker))

    def scan_gitleaks_report(self, path: Path) -> None:
        """A gitleaks JSON report as findings: rule, file, line and commit. The matched text, the secret, the author
        and the message in the report are never printed."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.report.errors.append(f"gitleaks report could not be read ({type(exc).__name__})")
            return
        for f in data or []:
            self.report.items += 1
            commit = str(f.get("Commit") or "")[:7]
            line = f.get("StartLine")
            self.add(self.safe(str(f.get("File") or "?")), line if isinstance(line, int) and line > 0 else None,
                     f"gitleaks: {str(f.get('RuleID') or '?')[:60]}", f"commit {commit}" if commit else "")

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
        # 2. the contents of blobs, annotated tags and commit objects, each once
        want = [sha for sha, (typ, _size) in types.items() if typ in ("blob", "tag", "commit")]
        paths_seen: set[str] = {p + "/" if types.get(sha, ("", 0))[0] == "tree" else p
                                for sha, p in objects.items() if p}
        flagged: list[str] = []
        before = len(self.report.findings)
        for sha, typ, data in self._read_objects(repo, want):
            path = objects.get(sha) or ""
            if typ == "commit":
                self._scan_commit(sha, data)
                continue
            label = path if typ == "blob" and path else f"annotated tag {sha[:7]}"
            if data is None:
                self.unscanned(label, "it is over the size limit; only its name was checked")
                continue
            mark = len(self.report.findings)
            self.scan_blob(label, data, extra=f"blob {sha[:7]}", path_checked=True)
            if len(self.report.findings) > mark:
                flagged.append(sha)
        # 3. every path ever touched (also by merge commits, and a blob that appears under several names)
        names = _git(repo, "log", "-m", "--name-only", "--format=", "-z", "--no-renames", *spec).split(b"\0")
        for n in names:
            if n:
                paths_seen.add(_dec(n).strip("\n"))
        for p in sorted(paths_seen):
            self.check_path(p)
        # 4. ref names
        for ref in _dec(_git(repo, "for-each-ref", "--format=%(refname)")).split("\n"):
            if ref:
                self.check_path(ref, prefix="ref ")
        # 5. which commit brought each flagged blob in (only for the first few)
        if flagged:
            self._name_commits(repo, flagged[:25], spec, before)

    def _scan_commit(self, sha: str, data: bytes | None) -> None:
        """A commit object as git stores it: author and committer (name, email), every other header (mergetag, a
        custom one ...) and the message."""
        who = f"commit {sha[:7]}"
        self.report.items += 1
        if data is None:
            self.unscanned(who, "it is over the size limit")
            return
        head, _, body = data.partition(b"\n\n")
        encoding = "utf-8"
        headers: list[tuple[str, str]] = []
        for raw in head.split(b"\n"):
            if raw.startswith(b" ") and headers:                       # a continuation line (gpgsig, mergetag)
                headers[-1] = (headers[-1][0], headers[-1][1] + "\n" + raw[1:].decode("utf-8", "replace"))
                continue
            bkey, _, bval = raw.partition(b" ")
            if bkey == b"encoding":
                encoding = bval.decode("ascii", "ignore").strip() or "utf-8"
            headers.append((bkey.decode("ascii", "replace"), bval.decode("utf-8", "replace")))

        def text(b: bytes) -> str:
            try:
                return b.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                return b.decode("utf-8", "replace")

        for key, val in headers:
            if key in ("tree", "parent", "encoding"):
                continue
            if key in ("author", "committer"):
                m = re.match(r"(.*?)\s*<(.*?)>", val, re.S)
                name, email = (m[1], m[2]) if m else (val, "")
                self.scan_text(f"{who} {key} name", name, one_line=True)
                self.scan_text(f"{who} {key} email", email, one_line=True)
            else:
                self.scan_text(f"{who} header {key}", val, one_line=True)
        self.scan_text(f"{who} message", text(body).rstrip("\n"))

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

    def _read_objects(self, repo: Path, shas: list[str]) -> Iterator[tuple[str, str, bytes | None]]:
        if not shas:
            return
        with tempfile.TemporaryFile() as f:
            f.write(("\n".join(shas) + "\n").encode())
            f.seek(0)
            proc = subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"], stdin=f, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, env=_git_env())
            assert proc.stdout is not None
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
                    if size > MAX_BYTES:
                        _discard(out, size + 1)
                        yield sha, typ, None
                        continue
                    data = out.read(size)
                    out.read(1)                                       # the newline after the content
                    yield sha, typ, data
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


_COMMENT_CHARS = "#;@!$%^&|:"                  # what core.commentChar=auto picks from
_CUT_LINE = "-" * 24 + " >8 " + "-" * 24


def _message_texts(data: bytes) -> list[str]:
    try:
        return [data.decode("utf-8")]
    except UnicodeDecodeError:                       # i18n.commitEncoding=ISO-8859-1 and the like
        return [data.decode("utf-8", "replace"), data.decode("latin-1")]


def _git_config(repo: Path | str, key: str) -> str | None:
    try:
        return _dec(_git(repo, "config", "--get", key)).strip() or None
    except GitError:                                 # not set (exit 1), or no git
        return None


def _git_appended(prepared: str, marker: str) -> list[str]:
    """The lines git appended to the message it handed to the editor (right-stripped): its comment block and, from its
    scissors line on, what -v shows. git cuts at that line only with -v or --cleanup=scissors, and then it wrote the
    line itself: below it there are only comment lines and the diff. A cut line that came with the message (-e -F)
    and has other text below it is not git's."""
    p = [ln.rstrip() for ln in prepared.split("\n")]
    while p and not p[-1]:
        p.pop()
    end = len(p)
    for i in range(len(p) - 1, -1, -1):
        if p[i] == f"{marker} {_CUT_LINE}":
            j = i + 1
            while j < len(p) and (not p[j] or p[j].startswith(marker)):
                j += 1
            if j == len(p) or p[j].startswith("diff --git "):
                end = i
            break
    start = end
    while start > 0 and (not p[start - 1] or p[start - 1].startswith(marker)):
        start -= 1
    return p[start:]


def _message_body(text: str, prepared: str | None = None, marker: str = "#") -> str:
    """The part of a commit message file that is scanned. Without `prepared` (no editor ran) all of it. With it,
    everything but the lines git appended to `prepared`, as long as they still end the file unchanged: a line that
    was typed is never left out, whatever it starts with."""
    if prepared is None:
        return text
    lines = text.split("\n")
    if marker == "auto":                             # git picked a sign no line of the message starts with
        last = next((ln for ln in reversed(prepared.split("\n")) if ln.strip()), "")
        marker = last[:1] if last[:1] in _COMMENT_CHARS else "#"
        for ln in prepared.split("\n"):
            if ln.rstrip().endswith(_CUT_LINE) and ln[:1] in _COMMENT_CHARS:
                marker = ln[:1]
    block = _git_appended(prepared, marker)
    seen = [ln.rstrip() for ln in lines]
    end = len(seen)
    while end and not seen[end - 1]:
        end -= 1
    k = 0
    while k < len(block) and k < end and seen[end - 1 - k] == block[-1 - k]:
        k += 1
    return "\n".join(lines[: end - k])


def _discard(stream, n: int) -> None:
    while n > 0:
        got = stream.read(min(n, 1 << 20))
        if not got:
            return
        n -= len(got)


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


# ---------------------------------------------------------------- hooks that also protect old branches

HOOK_NAMES = ("pre-commit", "pre-merge-commit", "commit-msg", "prepare-commit-msg", "_common.sh")


def install_hooks(repo: Path) -> str:
    """Copy the hooks and this scanner into the repository's shared git folder and point core.hooksPath at them
    (absolute, so every worktree and every branch uses them, also branches that predate scripts/hooks)."""
    here = Path(__file__).resolve().parent
    src = here / "hooks"
    common = Path(_dec(_git(repo, "rev-parse", "--git-common-dir")).strip())
    if not common.is_absolute():
        common = (Path(repo) / common).resolve()
    dest = common / "kairo-hooks"
    dest.mkdir(parents=True, exist_ok=True)
    for name in HOOK_NAMES:
        shutil.copyfile(src / name, dest / name)
        (dest / name).chmod((dest / name).stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    shutil.copyfile(Path(__file__).resolve(), dest / "privacy_scan.py")
    _git(repo, "config", "--local", "core.hooksPath", dest.as_posix())
    return str(dest)


# ---------------------------------------------------------------- command line

def _escape_data(s: str) -> str:
    """The message of a GitHub Actions workflow command."""
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(s: str) -> str:
    """A property (file=...) of a GitHub Actions workflow command."""
    return _escape_data(s).replace(":", "%3A").replace(",", "%2C")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="privacy_scan.py", description="Personal data and secrets must never get into the repo or a release.")
    p.add_argument("--staged", action="store_true", help="the lines the next commit adds")
    p.add_argument("--tree", nargs="?", const=".", metavar="DIR", help="a directory tree (default: .)")
    p.add_argument("--history", nargs="*", metavar="REF", help="every commit reachable from the refs (default: all refs)")
    p.add_argument("--zip", nargs="+", metavar="FILE", help="a release zip")
    p.add_argument("--message-file", metavar="FILE", help="a commit message (the commit-msg hook)")
    p.add_argument("--message-prepared", metavar="FILE",
                   help="with --message-file: the file as git handed it to the editor (the lines git appended are left out)")
    p.add_argument("--message-editor", action="store_true", help=argparse.SUPPRESS)       # hooks older than this scanner
    p.add_argument("--gitleaks-report", metavar="FILE", help="print a gitleaks JSON report (no matched text)")
    p.add_argument("--install-hooks", action="store_true", help="install the git hooks for every branch and worktree of --repo")
    p.add_argument("--ci", action="store_true", help="GitHub Actions: annotations; a missing list on main is an error")
    p.add_argument("--strict", action="store_true", help="content that cannot be scanned is an error (always with --ci and --zip)")
    p.add_argument("--secrets-only", action="store_true", help="do not check personal terms (another step does, with the list)")
    p.add_argument("--terms-file", metavar="FILE", help="the personal terms list (else see the module's help)")
    p.add_argument("--repo", metavar="DIR", default=".", help="the git repository for --staged and --history (default: .)")
    return p


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")       # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    env = os.environ if env is None else env
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.message_editor:
        print("error: the git hooks are older than this scanner. Run: python scripts/privacy_scan.py --install-hooks "
              "(or git config core.hooksPath scripts/hooks)", file=sys.stderr)
        return 2
    if args.install_hooks:
        try:
            print(f"privacy_scan: hooks installed in {install_hooks(Path(args.repo))} (core.hooksPath set for this repository)")
            return 0
        except (GitError, OSError) as exc:
            print(f"error: the hooks could not be installed ({exc})", file=sys.stderr)
            return 2
    if not (args.staged or args.tree is not None or args.history is not None or args.zip or args.message_file
            or args.gitleaks_report):
        parser.error("choose what to scan: --staged, --tree, --history, --zip, --message-file or --gitleaks-report")

    try:
        home: Path | None = Path.home()
    except (RuntimeError, KeyError):
        home = None
    source = TermsSource(None, "", [])
    if not args.secrets_only:
        try:
            source = load_terms(env, home, os.name == "nt", args.terms_file)
        except (TermError, TermsFileError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"error: the personal-terms list could not be read ({exc.strerror or type(exc).__name__})", file=sys.stderr)
            return 2

    scanner = Scanner(source.terms)
    report = scanner.report
    strict = args.ci or args.strict or bool(args.zip)
    config_errors: list[str] = []
    notes = list(source.warnings)
    on_main = env.get("GITHUB_REF") == "refs/heads/main"
    if args.secrets_only:
        notes.append("personal terms are not checked in this run (--secrets-only); secrets and paths are")
    elif source.terms is None:
        where = (f"{WINDOWS_DATA_DIR}\\config\\{TERMS_FILE_NAME}" if os.name == "nt"
                 else f"~/.config/kairo/{TERMS_FILE_NAME}")
        if args.ci and on_main:
            config_errors.append("no personal-terms list found, so the personal-terms check could not run. Add the "
                                 "repository secret PERSONAL_TERMS (one term per line) and pass it to this step as the "
                                 "environment variable KAIRO_PERSONAL_TERMS")
        elif args.ci:
            notes.append("no personal-terms list found: personal terms were NOT checked (secrets and paths were). Normal for a "
                         "pull request from a fork (forks get no secrets); otherwise add the repository secret PERSONAL_TERMS "
                         "(one term per line) and pass it to this step as KAIRO_PERSONAL_TERMS")
        else:
            notes.append("no personal-terms list found: personal terms were NOT checked (secrets and paths were). "
                         f"Put one term per line in {where}, or point KAIRO_PERSONAL_TERMS_FILE at a list")

    try:
        if args.staged:
            scanner.scan_staged(Path(args.repo))
        if args.message_file:
            scanner.scan_message_file(Path(args.message_file),
                                      Path(args.message_prepared) if args.message_prepared else None, Path(args.repo))
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
        if args.gitleaks_report:
            scanner.scan_gitleaks_report(Path(args.gitleaks_report))
    except GitError as exc:
        config_errors.append(str(exc))
    except OSError as exc:
        config_errors.append(f"could not read a file ({exc.strerror or type(exc).__name__})")

    config_errors += report.errors
    warnings = list(report.warnings)
    (config_errors if strict else warnings).extend(report.unscanned)
    findings = sorted(report.findings, key=lambda f: (f.where, f.line or 0, f.rule, f.extra))
    for w in notes + warnings:
        print(f"warning: {scanner.scrub(w)}", file=sys.stderr)
    config_errors = [scanner.scrub(e) for e in config_errors]
    for e in config_errors:
        print(f"error: {e}", file=sys.stderr)
    for f in findings[:MAX_LINES_PRINTED]:
        print(f.text())
    if len(findings) > MAX_LINES_PRINTED:
        print(f"... and {len(findings) - MAX_LINES_PRINTED} more")
    if args.ci and env.get("GITHUB_ACTIONS") == "true":
        for f in findings[:MAX_ANNOTATIONS]:
            if f.line:
                print(f"::error file={_escape_property(f.where)},line={f.line}::{_escape_data(f.rule)}")
            else:
                print(f"::error::{_escape_data(f.where + ': ' + f.rule)}")
        for e in config_errors:
            print(f"::error::{_escape_data(e)}")
        for w in notes:
            print(f"::warning::{_escape_data(scanner.scrub(w))}")
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
