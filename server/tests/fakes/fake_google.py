"""In-memory fake of the Gmail and Calendar APIs (httpx MockTransport).

Records every request in .calls so tests can check exactly what Kairo sent.
"""

from __future__ import annotations

import base64
import email
import json
from email import policy
from urllib.parse import parse_qs

import httpx

from app.google.client import save_token


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def decode_raw(raw: str) -> email.message.EmailMessage:
    data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    return email.message_from_bytes(data, policy=policy.default)


class FakeGoogle:
    def __init__(self, owner: str = "owner@example.com"):
        self.owner = owner
        self.calls: list[dict] = []
        self.messages: dict[str, dict] = {}
        self.labels: dict[str, str] = {"INBOX": "INBOX"}   # name -> id
        self.events: dict[str, dict] = {}
        self.drafts: list[dict] = []
        self.sent: list[dict] = []
        self.trashed: set[str] = set()
        self.deleted_events: list[tuple[str, str | None]] = []
        self.revoked = False
        self.grant_scope = " ".join([
            "https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/gmail.compose",
            "https://www.googleapis.com/auth/calendar.events"])
        self._next = 1

    # ---- seed

    def add_message(self, mid: str, *, sender: str, subject: str, body: str, thread: str | None = None,
                    reply_to: str | None = None, message_id_header: str | None = None, to: str | None = None,
                    cc: str | None = None) -> None:
        headers = [{"name": "From", "value": sender}, {"name": "To", "value": to or self.owner},
                   {"name": "Subject", "value": subject}, {"name": "Date", "value": "Mon, 12 Oct 2026 09:00:00 +0200"},
                   {"name": "Message-ID", "value": message_id_header or f"<{mid}@mail.example>"}]
        if reply_to:
            headers.append({"name": "Reply-To", "value": reply_to})
        if cc:
            headers.append({"name": "Cc", "value": cc})
        self.messages[mid] = {"id": mid, "threadId": thread or f"t-{mid}", "labelIds": ["INBOX"], "snippet": body[:80],
                              "internalDate": "1760252400000",   # 2026-10-12
                              "payload": {"mimeType": "text/plain", "headers": headers, "body": {"data": b64(body)}}}

    def add_event(self, eid: str, *, summary: str, start: str, attendees: list[dict] | None = None, organizer: str | None = None):
        self.events[eid] = {"id": eid, "summary": summary, "start": {"dateTime": start}, "end": {"dateTime": start},
                            "attendees": attendees or [], "organizer": {"email": organizer or self.owner}}

    # ---- transport

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = parse_qs(request.content.decode())
        params = dict(request.url.params)
        path = request.url.path
        self.calls.append({"method": request.method, "path": path, "params": params, "body": body,
                           "auth": request.headers.get("authorization")})
        m = request.method

        if request.url.host == "oauth2.googleapis.com":
            if self.revoked:
                return httpx.Response(400, json={"error": "invalid_grant"})
            if body and body.get("grant_type") == ["authorization_code"]:
                if body.get("code") != ["good-code"] or not body.get("code_verifier"):
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json={"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3600,
                                                 "scope": self.grant_scope})
            return httpx.Response(200, json={"access_token": "at-" + str(len(self.calls)), "expires_in": 3600})

        if self.revoked:   # like Google: a revoked grant's access tokens stop working too
            return httpx.Response(401, json={"error": {"message": "Invalid Credentials"}})
        g = "/gmail/v1/users/me"
        if path == f"{g}/profile":
            return httpx.Response(200, json={"emailAddress": self.owner})
        if path == f"{g}/messages" and m == "GET":
            ids = [{"id": k, "threadId": v["threadId"]} for k, v in self.messages.items() if k not in self.trashed]
            return httpx.Response(200, json={"messages": ids[: int(params.get("maxResults", 10))]})
        if path == f"{g}/messages/send" and m == "POST":
            self.sent.append(body)
            sid = f"sent{len(self.sent)}"
            import time as _t
            raw = decode_raw(body["raw"])
            self.messages[sid] = {"id": sid, "threadId": body.get("threadId", "t"), "labelIds": ["SENT"],
                                  "internalDate": str(int(_t.time() * 1000)),
                                  "payload": {"headers": [{"name": "From", "value": self.owner}, {"name": "To", "value": raw["To"] or ""}]}}
            return httpx.Response(200, json={"id": sid, "threadId": body.get("threadId", "t")})
        if path.startswith(f"{g}/threads/") and m == "GET":
            tid = path[len(f"{g}/threads/"):]
            msgs = [v for v in self.messages.values() if v.get("threadId") == tid]
            return httpx.Response(200, json={"id": tid, "messages": msgs}) if msgs else httpx.Response(404, json={"error": {"message": "Not Found"}})
        if path.startswith(f"{g}/messages/"):
            rest = path[len(f"{g}/messages/"):]
            mid, _, action = rest.partition("/")
            msg = self.messages.get(mid)
            if msg is None:
                return httpx.Response(404, json={"error": {"message": "Not Found"}})
            if action == "" and m == "GET":
                return httpx.Response(200, json=msg)
            if action == "modify" and m == "POST":
                labels = set(msg["labelIds"]) | set(body.get("addLabelIds", []))
                labels -= set(body.get("removeLabelIds", []))
                msg["labelIds"] = sorted(labels)
                return httpx.Response(200, json=msg)
            if action == "trash" and m == "POST":
                self.trashed.add(mid)
                return httpx.Response(200, json=msg)
        if path == f"{g}/labels" and m == "GET":
            return httpx.Response(200, json={"labels": [{"id": i, "name": n} for n, i in self.labels.items()]})
        if path == f"{g}/labels" and m == "POST":
            lid = f"Label_{len(self.labels)}"
            self.labels[body["name"]] = lid
            return httpx.Response(200, json={"id": lid, "name": body["name"]})
        if path == f"{g}/drafts" and m == "POST":
            self.drafts.append(body)
            return httpx.Response(200, json={"id": f"d{len(self.drafts)}"})

        c = "/calendar/v3/calendars/primary/events"
        if path == c and m == "GET":
            return httpx.Response(200, json={"items": list(self.events.values())})
        if path == c and m == "POST":
            eid = f"ev{self._next}"
            self._next += 1
            self.events[eid] = {**body, "id": eid, "_sendUpdates": params.get("sendUpdates")}
            return httpx.Response(200, json=self.events[eid])
        if path.startswith(c + "/"):
            eid = path[len(c) + 1:]
            ev = self.events.get(eid)
            if ev is None:
                return httpx.Response(404, json={"error": {"message": "Not Found"}})
            if m == "GET":
                return httpx.Response(200, json=ev)
            if m == "PATCH":
                ev.update(body)
                ev["_sendUpdates"] = params.get("sendUpdates")
                return httpx.Response(200, json=ev)
            if m == "DELETE":
                self.deleted_events.append((eid, params.get("sendUpdates")))
                del self.events[eid]
                return httpx.Response(204)
        return httpx.Response(404, json={"error": {"message": f"fake: no route {m} {path}"}})


def connect(secrets_dir, fake: FakeGoogle | None = None) -> None:
    """Write a fake OAuth client + both tokens into secrets_dir."""
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / "google-client.json").write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))
    owner = fake.owner if fake else "owner@example.com"
    for kind in ("read", "write"):
        save_token(secrets_dir, kind, {"refresh_token": f"rt-{kind}", "email": owner})
