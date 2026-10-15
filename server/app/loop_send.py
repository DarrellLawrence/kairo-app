"""Loop Send (fix P1.6/7, 2026-10-14): where a ready answer may go, and the card for it.

Only to a verified address (CLAUDE.md: untrusted content never creates or approves a card):
- a reply to a mail: that mail's real Reply-To/From header (as for any gmail.reply card);
- a promise found in a sent mail: that mail's real To/Cc addresses, never the owner's. With several
  people on it and none named for sure, the owner picks one or more (name + full address shown;
  loops block L, 2026-10-15: no more "send it yourself");
- anything else (birthday, people, ...): an address the owner typed in the app or confirmed
  once, after seeing it in full. An address the model wrote (people_note, promise finder) is
  never used on its own.

The owner always sees the address before sending (preview); the send must name the address
that was shown. The card then goes the same way as every card (gate, kill switch, limits,
tier-4 confirm), with the owner as actor, and is decided at once: it never stays open.
"""

from __future__ import annotations

import datetime as dt
import json
from email.utils import getaddresses

from sqlmodel import Session

from . import life
from .db import OpenLoop, SettingKV

VERIFIED_KEY = "contacts.verified"


class SendRefused(ValueError):
    """Shown to the owner as is."""


def _norm(address: str | None) -> str:
    return (address or "").strip().lower()


def verified_addresses(s: Session) -> dict[str, dict]:
    row = s.get(SettingKV, VERIFIED_KEY)
    try:
        data = json.loads(row.value) if row and row.value else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def mark_verified(s: Session, address: str, how: str) -> None:
    """Only from an owner action in the app (typed it, or confirmed it after seeing it in full)."""
    addr = _norm(address)
    if "@" not in addr:
        return
    data = verified_addresses(s)
    data[addr] = {"how": how[:120], "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    row = s.get(SettingKV, VERIFIED_KEY) or SettingKV(key=VERIFIED_KEY)
    row.value = json.dumps(data)
    s.add(row)
    s.commit()


def is_verified(s: Session, address: str | None) -> bool:
    return bool(address) and _norm(address) in verified_addresses(s)


def _headers(google_read, message_id: str, names: list[str]) -> dict[str, str]:
    g = google_read()
    try:
        msg = g.get(f"/gmail/v1/users/me/messages/{message_id}", format="metadata", metadataHeaders=names)
    finally:
        g.close()
    return {h["name"].lower(): h["value"] for h in (msg.get("payload") or {}).get("headers", [])}


def _addresses(*values: str | None) -> list[str]:
    out: list[str] = []
    for _, addr in getaddresses([v for v in values if v]):
        a = _norm(addr)
        if "@" in a and a not in out:
            out.append(a)
    return out


def promise_people(loop: OpenLoop, *, google_read, owner_email: str | None) -> list[dict]:
    """[{name, email}]: the other people on the mail a promise came from (stored when it was found;
    older loops: read from the mail again). Never the owner."""
    from . import loops
    from .identity import people_in
    people = loops.people_of(loop)
    if people:
        return people
    ref = loop.mail_ref or ((loop.source_ref or "").split(":", 1)[0] if not (loop.source_ref or "").startswith("p:") else "")
    if not ref:
        return []
    h = _headers(google_read, ref, ["To", "Cc"])
    return [p for p in people_in(h.get("to"), h.get("cc")) if p["email"] != _norm(owner_email)]


def plan(s: Session, loop: OpenLoop, *, google_read, text: str, subject: str | None, owner_email: str | None,
         chosen: list[str] | None = None) -> dict:
    """What a Send would do: {action, args, to, recipients, verified, how}, or {pick: [{name, email}]} when
    the owner has to choose who gets it. chosen: the addresses the owner picked. Raises SendRefused."""
    if loop.source == "email" and loop.kind in ("reply", "waiting") and loop.source_ref:
        h = _headers(google_read, loop.source_ref, ["From", "Reply-To"])
        to = (_addresses(h.get("reply-to")) or _addresses(h.get("from")) or [None])[0]
        if not to:
            raise SendRefused("Kairo can't tell who sent that mail. Answer it in Gmail and tap Done.")
        return {"action": "gmail.reply", "args": {"message_id": loop.source_ref, "body": text}, "to": to,
                "recipients": [to], "verified": True, "how": "the sender of the mail you're answering"}

    subject = (subject or loop.what or "")[:200].replace("\n", " ")
    people = promise_people(loop, google_read=google_read, owner_email=owner_email) if loop.source == "promise" else []
    if people:
        real = [p["email"] for p in people]
        if chosen is not None:
            picked = [_norm(a) for a in chosen]
            if not picked or any(a not in real for a in picked):
                raise SendRefused("Pick people who were on that mail.")
            to_list = list(dict.fromkeys(picked))
        elif _norm(loop.who_email) in real:
            to_list = [_norm(loop.who_email)]
        elif len(real) == 1:
            to_list = real
        else:
            return {"pick": people, "action": "gmail.send"}
        return {"action": "gmail.send", "args": {"to": to_list, "subject": subject, "body": text}, "to": to_list[0],
                "recipients": to_list, "verified": True, "how": "on the mail you made the promise in"}
    else:
        person = life.person_find(s, loop.who) if loop.who and loop.source in ("birthday", "people") else None
        to = _norm(person.email if person and person.email else loop.who_email) or None
        if not to:
            raise SendRefused("Kairo doesn't know where to send this. Do it yourself and tap Done.")
        if not is_verified(s, to):
            return {"action": "gmail.send", "args": {"to": [to], "subject": subject, "body": text}, "to": to,
                    "recipients": [to], "verified": False, "how": "Kairo noted this address; check it before the first mail"}
        how = "an address you set or confirmed"
    return {"action": "gmail.send", "args": {"to": [to], "subject": subject, "body": text}, "to": to,
            "recipients": [to], "verified": True, "how": how}
