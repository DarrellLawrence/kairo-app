"""What Kairo's browser may ever load on a school portal (owner's hard rule, 2026-10-14).

Never open, start, answer or submit tests, quizzes or assignments; never click
submit buttons; never post anything. Enforced in three layers:
1. The reader has no click, fill or post: it only opens pages by address and
   downloads files (SafeDriver). The only form it ever fills is the login.
2. Only reading course parts are opened (allow-list of OLAT node types);
   tests, tasks, surveys, forums, enrollment and anything unknown are skipped.
3. The browser itself aborts every request that isn't a page load (GET/HEAD)
   after login, any address on another site, and any test/quiz/assignment URL.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

FORBIDDEN_URL = re.compile(
    r"(/tests?(/|$)|iqtest|iqself|iqsurv|qti|assessment|/quiz|/survey|/exam|/submit|/upload|/edit|/delete|"
    r"/forum|/gta|/ita(/|$)|/task|/enrol|/einschreib|/abgabe|/start(/|$)|/answer|/post(/|$)|/new(/|$))", re.I)
# OLAT course node types (icon class o_<type>_icon) that only show content.
SAFE_NODES = {"bc", "sp", "st", "info", "cal", "ll", "cp", "document"}
NODE_ICON = re.compile(r"\bo_([a-z0-9]+)_icon\b")
NOT_NODE = {"filetype", "icon"}


LOCAL = ("127.0.0.1", "localhost")   # a fake portal in tests only


def _same_site(url: str, base: str) -> bool:
    u, b = urlsplit(url), urlsplit(base)
    secure = b.scheme == "https" or (b.scheme == "http" and b.hostname in LOCAL)
    return secure and u.scheme == b.scheme and u.hostname == b.hostname and u.port == b.port


def url_allowed(url: str, base: str) -> bool:
    if not _same_site(url, base):
        return False
    path = unquote(urlsplit(url).path)
    return not FORBIDDEN_URL.search(path)


def request_allowed(method: str, url: str, base: str, *, phase: str) -> bool:
    """phase "login": the login form may be posted (same site only); "read": page loads only."""
    if not url_allowed(url, base):
        return False
    m = method.upper()
    if m in ("GET", "HEAD"):
        return True
    return phase == "login" and m == "POST"


def node_type(classes: str | None) -> str | None:
    for t in NODE_ICON.findall(classes or ""):
        if t not in NOT_NODE and not t.startswith("filetype"):
            return t
    return None


def node_allowed(kind: str | None) -> bool:
    return kind in SAFE_NODES
