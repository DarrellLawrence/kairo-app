"""Who the owner is (loops block L, owner's request 2026-10-15).

Every name and address that means the owner: profile.yaml on the PC (name, gmail, emails, aliases)
plus what Google says (the account's address, every Gmail "send as" address and its display name).
Kairo never makes a loop, a draft or a card addressed to the owner, and a promise is always to the
other people on the mail. Nothing personal lives in the repo: the values come from the PC.
Google is asked at most once a day; what it said is kept (SettingKV) for when it is down.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from email.utils import getaddresses

from sqlmodel import Session

from .db import SettingKV

log = logging.getLogger("kairo.identity")

KEY = "identity.owner"
REFRESH = dt.timedelta(hours=24)


def _norm_name(name: str | None) -> str:
    """'Rivera, Sam' / 'SAM  RIVERA' / 'Sám Rivera' -> 'rivera sam' (sorted words, no accents, no case)."""
    t = unicodedata.normalize("NFKD", name or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    words = re.findall(r"[a-z0-9]+", t)
    return " ".join(sorted(words))


def _norm_email(address: str | None) -> str:
    return (address or "").strip().strip("<>").lower()


def people_in(*headers: str | None) -> list[dict]:
    """[{name, email}] from To/Cc/From header values, in order, each address once."""
    out, seen = [], set()
    for name, addr in getaddresses([h for h in headers if h]):
        a = _norm_email(addr)
        if "@" not in a or a in seen:
            continue
        seen.add(a)
        out.append({"name": (name or "").strip().strip('"') or a.split("@")[0], "email": a})
    return out


@dataclass
class Owner:
    names: set[str] = field(default_factory=set)        # normalised full names and aliases
    first_names: set[str] = field(default_factory=set)  # only enough when no address says otherwise
    emails: set[str] = field(default_factory=set)
    display: list[str] = field(default_factory=list)    # the names as written, for prompts

    def is_me(self, *, email: str | None = None, name: str | None = None) -> bool:
        e = _norm_email(email)
        if e:
            if e in self.emails:
                return True
            if "@" in e:                   # someone with their own address is not the owner, whatever the name
                return False
        n = _norm_name(name)
        return bool(n) and (n in self.names or n in self.first_names)

    def others(self, people: list[dict]) -> list[dict]:
        return [p for p in people if not self.is_me(email=p.get("email"), name=None if p.get("email") else p.get("name"))]

    def describe(self) -> str:
        """For a model prompt: who the owner is (their own data, shown to their own Claude)."""
        return f"{' / '.join(self.display) or '?'} ({', '.join(sorted(self.emails)) or 'no address known'})"


def _from_google(google_read) -> dict:
    g = google_read()
    try:
        prof = g.get("/gmail/v1/users/me/profile")
        send_as = g.get("/gmail/v1/users/me/settings/sendAs").get("sendAs", []) or []
    finally:
        g.close()
    emails = {_norm_email(prof.get("emailAddress"))} | {_norm_email(x.get("sendAsEmail")) for x in send_as}
    names = {str(x.get("displayName") or "").strip() for x in send_as}
    return {"emails": sorted(e for e in emails if "@" in e), "names": sorted(n for n in names if n)}


def load(s: Session, profile, *, google_read=None, now: dt.datetime | None = None, refresh: bool = False) -> Owner:
    now = now or dt.datetime.now(dt.timezone.utc)
    row = s.get(SettingKV, KEY)
    try:
        cached = json.loads(row.value) if row and row.value else {}
    except ValueError:
        cached = {}
    stale = refresh or not cached.get("at") or now - dt.datetime.fromisoformat(cached["at"]) > REFRESH
    if google_read is not None and stale:
        try:
            got = _from_google(google_read)
            cached = {**got, "at": now.isoformat()}
            row = row or SettingKV(key=KEY)
            row.value = json.dumps(cached)
            s.add(row)
            s.commit()
        except Exception as exc:   # Google down or not connected: what it said before still counts
            log.info("owner identity from Google: %s", exc)
    me = Owner()
    full = [profile.name, *(getattr(profile, "aliases", None) or []), *cached.get("names", [])]
    for n in full:
        if _norm_name(n):
            if _norm_name(n) not in me.names:
                me.display.append(n.strip())
            me.names.add(_norm_name(n))
            first = re.findall(r"[^\W\d_]+", unicodedata.normalize("NFKC", n))
            if first:
                me.first_names.add(_norm_name(first[0]))
    for e in [profile.gmail, *(getattr(profile, "emails", None) or []), *cached.get("emails", [])]:
        if "@" in _norm_email(e):
            me.emails.add(_norm_email(e))
    return me
