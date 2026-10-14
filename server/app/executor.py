"""The ONLY place Kairo writes to Google (CLAUDE.md). Runs tier-2 actions right
after the gate allowed them, and tier 3-4 actions after the owner approved the
card, from the stored args. The model never reaches this code directly and
never sees the write token.
"""

from __future__ import annotations

import base64
import mimetypes
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Callable

import httpx

from .google.client import GoogleAPI

KAIRO_HOLD_PREFIX = "[Kairo] "


class Executor:
    def __init__(self, *, secrets_dir: Path, transport: httpx.BaseTransport | None = None,
                 owner_email: str | None = None, event_type: Callable[[str], str] | None = None,
                 vault_dir: Path | None = None):
        self.secrets_dir = Path(secrets_dir)
        self.vault_dir = Path(vault_dir) if vault_dir else None   # gmail.send_self attachments come only from here
        self._transport = transport
        self.owner_email = owner_email   # profile.gmail: the only address gmail.send_self can reach
        self.event_type = event_type or (lambda event_id: "FIXED")   # Kairo's calendar tags (v7)

    def _write_api(self) -> GoogleAPI:
        return GoogleAPI(self.secrets_dir, "write", transport=self._transport)

    def run(self, action: str, args: dict) -> dict:
        handler: Callable[[GoogleAPI, dict], dict] | None = getattr(self, "_" + action.replace(".", "_"), None)
        if handler is None:
            raise ValueError(f"executor has no action {action!r}")
        g = self._write_api()
        try:
            return handler(g, args)
        finally:
            g.close()

    # ---------------------------------------------------------------- tier 2

    def _gmail_label(self, g: GoogleAPI, a: dict) -> dict:
        existing = {l["name"]: l["id"] for l in g.get("/gmail/v1/users/me/labels").get("labels", [])}

        def label_id(name: str) -> str:
            if name not in existing:
                existing[name] = g.request("POST", "/gmail/v1/users/me/labels",
                                           json_body={"name": name, "labelListVisibility": "labelShow",
                                                      "messageListVisibility": "show"})["id"]
            return existing[name]

        body = {"addLabelIds": [label_id(n) for n in a["add"]], "removeLabelIds": [existing[n] for n in a["remove"] if n in existing]}
        g.request("POST", f"/gmail/v1/users/me/messages/{a['message_id']}/modify", json_body=body)
        return {"labelled": a["message_id"], "add": a["add"], "remove": a["remove"]}

    def _gmail_create_draft(self, g: GoogleAPI, a: dict) -> dict:
        message: dict[str, Any] = {"raw": _raw(to=a["to"], cc=a["cc"], subject=a["subject"], body=a["body"])}
        if a.get("reply_to_message_id"):
            orig = g.get(f"/gmail/v1/users/me/messages/{a['reply_to_message_id']}", format="metadata",
                         metadataHeaders=["Message-ID", "References"])
            h = {x["name"].lower(): x["value"] for x in orig["payload"]["headers"]}
            message = {"raw": _raw(to=a["to"], cc=a["cc"], subject=a["subject"], body=a["body"],
                                   in_reply_to=h.get("message-id"),
                                   references=_refs(h.get("references"), h.get("message-id"))),
                       "threadId": orig.get("threadId")}
        draft = g.request("POST", "/gmail/v1/users/me/drafts", json_body={"message": message})
        return {"draft_id": draft.get("id")}

    def _calendar_create_hold(self, g: GoogleAPI, a: dict) -> dict:
        # Built field by field from the allow-list; sendUpdates=none on top.
        event: dict[str, Any] = {"summary": KAIRO_HOLD_PREFIX + a["summary"],
                                 "start": {"dateTime": a["start"]}, "end": {"dateTime": a["end"]}}
        for key in ("description", "location", "reminders", "colorId", "transparency"):
            if a.get(key) is not None:
                event[key] = a[key]
        if a.get("recurrence"):   # weekly with an end (checked by the schema); Google needs the zone name
            event["recurrence"] = list(a["recurrence"])
            event["start"]["timeZone"] = event["end"]["timeZone"] = a["time_zone"]
        created = g.request("POST", "/calendar/v3/calendars/primary/events", params={"sendUpdates": "none"}, json_body=event)
        return {"event_id": created.get("id"), "summary": event["summary"]}

    def _gmail_send_self(self, g: GoogleAPI, a: dict) -> dict:
        # The recipient comes from the profile, never from the args (there is no field for it).
        if not self.owner_email:
            raise ValueError("no own address in config/profile.yaml (gmail:)")
        files = [self._vault_file(p) for p in a.get("attachments") or []]
        if sum(len(data) for _, data in files) > MAX_ATTACH:
            raise ValueError("attachments too big for one mail (25 MB)")
        sent = g.request("POST", "/gmail/v1/users/me/messages/send",
                         json_body={"raw": _raw(to=[self.owner_email], cc=[], subject=a["subject"], body=a["body"],
                                                files=files)})
        if a.get("label"):
            self._gmail_label(g, {"message_id": sent["id"], "add": [a["label"]], "remove": []})
        return {"sent_id": sent.get("id"), "to": "owner"}

    def _vault_file(self, rel: str) -> tuple[str, bytes]:
        if self.vault_dir is None:
            raise ValueError("no vault configured for attachments")
        root = self.vault_dir.resolve()
        target = (root / rel.replace("\\", "/")).resolve()   # symlinks resolved: must still be inside the vault
        if root not in target.parents:
            raise ValueError(f"attachment outside the vault: {rel}")
        if not target.is_file():
            raise ValueError(f"attachment not found in the vault: {rel}")
        return target.name, target.read_bytes()

    def _calendar_move(self, g: GoogleAPI, a: dict) -> dict:
        kind = self.event_type(a["event_id"])
        if kind != "FLEXIBLE":
            raise ValueError(f"this event is {kind}: Kairo only moves FLEXIBLE events")
        event = g.get(f"/calendar/v3/calendars/primary/events/{a['event_id']}")
        if event.get("attendees"):
            raise ValueError("this event has guests: moving it would notify them (propose a message instead)")
        g.request("PATCH", f"/calendar/v3/calendars/primary/events/{a['event_id']}", params={"sendUpdates": "none"},
                  json_body={"start": {"dateTime": a["start"]}, "end": {"dateTime": a["end"]}})
        return {"moved": a["event_id"], "start": a["start"], "end": a["end"]}

    # ---------------------------------------------------------------- tier 3

    def _gmail_send(self, g: GoogleAPI, a: dict) -> dict:
        sent = g.request("POST", "/gmail/v1/users/me/messages/send",
                         json_body={"raw": _raw(to=a["to"], cc=a.get("cc", []), subject=a["subject"], body=a["body"])})
        return {"sent_id": sent.get("id")}

    def _gmail_reply(self, g: GoogleAPI, a: dict) -> dict:
        # Uses only what the card stored (prepare_reply): approved = exact.
        raw = _raw(to=a["to"], cc=[], subject=a["subject"], body=a["body"],
                   in_reply_to=a.get("in_reply_to"), references=a.get("references"))
        sent = g.request("POST", "/gmail/v1/users/me/messages/send", json_body={"raw": raw, "threadId": a["thread_id"]})
        return {"sent_id": sent.get("id")}

    def _calendar_respond(self, g: GoogleAPI, a: dict) -> dict:
        event = g.get(f"/calendar/v3/calendars/primary/events/{a['event_id']}")
        attendees = event.get("attendees") or []
        mine = [x for x in attendees if x.get("self")]
        if not mine:
            raise ValueError("you are not an attendee of this event")
        for x in mine:
            x["responseStatus"] = a["response"]
        g.request("PATCH", f"/calendar/v3/calendars/primary/events/{a['event_id']}",
                  params={"sendUpdates": "all"}, json_body={"attendees": attendees})
        return {"event_id": a["event_id"], "response": a["response"]}

    # ---------------------------------------------------------------- trash (tier 2 since v7: 30 days to undo)

    def _gmail_trash(self, g: GoogleAPI, a: dict) -> dict:
        g.request("POST", f"/gmail/v1/users/me/messages/{a['message_id']}/trash")
        return {"trashed": a["message_id"]}

    # ---------------------------------------------------------------- tier 4

    def _calendar_delete(self, g: GoogleAPI, a: dict) -> dict:
        g.request("DELETE", f"/calendar/v3/calendars/primary/events/{a['event_id']}",
                  params={"sendUpdates": a.get("send_updates", "none")})
        return {"deleted": a["event_id"]}


def _refs(existing: str | None, message_id: str | None) -> str | None:
    parts = [p for p in [(existing or "").strip(), (message_id or "").strip()] if p]
    return " ".join(parts) or None


MAX_ATTACH = 25 * 1024 * 1024


def _raw(*, to: list[str], cc: list[str], subject: str, body: str,
         in_reply_to: str | None = None, references: str | None = None,
         files: list[tuple[str, bytes]] | None = None) -> str:
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    for name, data in files or []:
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")


def first_address(header_value: str | None) -> str | None:
    pairs = getaddresses([header_value or ""])
    return pairs[0][1] if pairs and pairs[0][1] else None
