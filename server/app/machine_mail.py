"""Mail a machine sent (owner's report 2026-10-20: "kinda annoying"; a real person's mail was "a good catch").

A no-reply, notification or newsletter sender, mass mail (List-Unsubscribe / List-Id / Precedence bulk or list),
mail with Auto-Submitted other than "no", and what the triage model sorted as automatic mail ("ignore") never
cause a push: not the suspicious-mail flag, not a reply loop, not a task, not a line in the brief. Such mail is
still triaged, labelled and listed in the app. Mail from a real person notifies as before.

This is the one decision; triage and loops both ask it. The triage has no category for a security alert about
the owner's own account (sign-in, password, 2FA codes): those come from machines too, so they never push.
"""

from __future__ import annotations

import re
from typing import Callable, Mapping

AUTOMATED_LOCAL = re.compile(r"^(no-?reply|do-?not-?reply|donotreply|noreply|notifications?|newsletters?|news|marketing|"
                             r"promo(tions)?|mailer(-daemon)?|bounces?|updates?|alerts?|digest)([+.-].*)?$", re.I)   # info@, hello@, team@: by headers
NO_REPLY = re.compile(r"no-?reply|do-?not-?reply", re.I)   # anywhere in the address: orders-noreply@, account-security-noreply@
AUTOMATED_DOMAIN = re.compile(r"(mailchimp|sendgrid|mailgun|mcsv\.net|amazonses|sparkpost|hubspot|klaviyo|brevo|sendinblue|"
                              r"constantcontact|mail\.instagram|facebookmail|linkedin\.com|accounts\.google)", re.I)
BULK_HEADERS = ("list-unsubscribe", "list-id")
BULK_PRECEDENCE = ("bulk", "list", "junk")
HEADER_NAMES = ("List-Unsubscribe", "List-Id", "Precedence", "Auto-Submitted")   # the headers this decision reads
AUTOMATIC_CATEGORY = "ignore"   # the triage model's sort: newsletters, ads, automatic mail


def address(sender: str | None) -> str | None:
    """The address in a From line ("Sam <sam@example.com>" -> "sam@example.com"), lowercase."""
    m = re.search(r"<([^>]+)>", sender or "")
    return (m.group(1) if m else (sender or "")).strip().lower() or None


def automated_sender(sender: str | None) -> bool:
    """No-reply, notification, newsletter and promo senders and the mass-mail services. Real people, school, work
    are not (info@, hello@, team@ can be a person: their headers decide)."""
    local, _, domain = (address(sender) or "").partition("@")
    return bool(AUTOMATED_LOCAL.match(local) or NO_REPLY.search(local) or AUTOMATED_DOMAIN.search(domain))


def automated_headers(headers: Mapping[str, str] | None) -> str | None:
    """Why the headers say a machine sent it (mass mail, auto-submitted), or None. Header names in any case."""
    h = {k.lower(): v for k, v in (headers or {}).items()}
    if any(k in h for k in BULK_HEADERS) or (h.get("precedence") or "").strip().lower() in BULK_PRECEDENCE:
        return "bulk mail headers"
    auto = re.split(r"[\s;(]", (h.get("auto-submitted") or "no").strip().lower(), maxsplit=1)[0]
    return None if auto in ("", "no") else "auto-submitted"


def sent_by_machine(sender: str | None = None,
                    headers: Mapping[str, str] | Callable[[], Mapping[str, str] | None] | None = None,
                    category: str | None = None) -> str | None:
    """Why this mail was sent by a machine, or None (a person may have written it): then it may notify.
    headers: the mail's headers, or a function that reads them (from Gmail), called only when the sender and the
    triage's sort didn't decide. category: the triage model's sort (reply / fyi / ignore)."""
    if automated_sender(sender):
        return "no-reply or notification sender"
    if category == AUTOMATIC_CATEGORY:
        return "sorted as automatic mail"
    return automated_headers(headers() if callable(headers) else headers)
