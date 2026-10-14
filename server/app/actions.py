"""Argument schemas for every action the model can request (KICKOFF 8).

Every schema forbids unknown fields: the gate rejects anything it was not
built to understand. An action without a schema here cannot be decided, so it
is denied.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_EMAIL = re.compile(r"^[^@\s<>,;\"]+@[^@\s<>,;\"]+\.[^@\s<>,;\"]+$")
_GOOGLE_ID = r"^[A-Za-z0-9_\-@.]{1,256}$"
KAIRO_LABEL_PREFIX = "Kairo/"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # no stripping: bodies and notes must stay exactly as written


def _one_line(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError("must be a single line")
    return value


def _check_emails(values: list[str]) -> list[str]:
    for v in values:
        if not _EMAIL.match(v):
            raise ValueError(f"not an email address: {v!r}")
    return values


# ---------------------------------------------------------------- tier 0: read

class GmailSearch(Strict):
    query: str = Field(max_length=500)
    max_results: int = Field(default=10, ge=1, le=25)


class GmailRead(Strict):
    message_id: str = Field(pattern=_GOOGLE_ID)


class CalendarList(Strict):
    time_min: datetime | None = None
    time_max: datetime | None = None
    max_results: int = Field(default=25, ge=1, le=100)


class VaultRead(Strict):
    path: str = Field(default="", max_length=500)


class TasksList(Strict):
    status: Literal["open", "done", "dropped"] | None = "open"


# ---------------------------------------------------------------- tier 1: internal

_PLACE = r"^[\w][\w .'&-]{0,39}$"


class TasksCreate(Strict):
    title: str = Field(min_length=1, max_length=300)
    due: datetime | None = None
    priority: int = Field(default=0, ge=0, le=3)
    project_id: int | None = None
    place: str | None = Field(default=None, pattern=_PLACE)   # remind on arrival there (Home, School, Work, Gym)


class TasksUpdate(Strict):
    id: int
    title: str | None = Field(default=None, min_length=1, max_length=300)
    due: datetime | None = None
    priority: int | None = Field(default=None, ge=0, le=3)
    status: Literal["open", "done", "dropped"] | None = None
    place: str | None = Field(default=None, pattern=_PLACE)


class NotesWrite(Strict):
    path: str = Field(min_length=1, max_length=300)  # inside the vault's Kairo-owned folder
    content: str = Field(max_length=100_000)
    mode: Literal["overwrite", "append"] = "overwrite"


# ---------------------------------------------------------------- tier 2: reversible, nobody notified

class GmailLabel(Strict):
    message_id: str = Field(pattern=_GOOGLE_ID)
    add: list[str] = Field(default_factory=list, max_length=5)
    remove: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def only_kairo_labels(self):
        if not self.add and not self.remove:
            raise ValueError("nothing to change")
        for label in self.add + self.remove:
            if not label.startswith(KAIRO_LABEL_PREFIX) or len(label) > 100:
                raise ValueError(f"Kairo may only change its own labels ({KAIRO_LABEL_PREFIX}...), not {label!r}")
        return self


class GmailCreateDraft(Strict):
    to: list[str] = Field(min_length=1, max_length=10)
    cc: list[str] = Field(default_factory=list, max_length=10)
    subject: str = Field(max_length=300)
    body: str = Field(max_length=50_000)
    reply_to_message_id: str | None = Field(default=None, pattern=_GOOGLE_ID)

    _emails = field_validator("to", "cc")(_check_emails)
    _subject = field_validator("subject")(_one_line)


class CalendarCreateHold(Strict):
    """Own calendar only. Fields are an allow-list (KICKOFF 8): anything that
    could notify other people simply has no field here."""

    summary: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5_000)
    start: datetime
    end: datetime
    location: str | None = Field(default=None, max_length=300)
    reminders: dict | None = None
    colorId: str | None = Field(default=None, pattern=r"^\d{1,2}$")
    transparency: Literal["opaque", "transparent"] | None = None

    @model_validator(mode="after")
    def end_after_start(self):
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("start and end need a time zone")
        return self


# ---------------------------------------------------------------- tier 3: outbound

class GmailSend(Strict):
    to: list[str] = Field(min_length=1, max_length=10)
    cc: list[str] = Field(default_factory=list, max_length=10)
    subject: str = Field(max_length=300)
    body: str = Field(max_length=50_000)

    _emails = field_validator("to", "cc")(_check_emails)
    _subject = field_validator("subject")(_one_line)


class GmailReply(Strict):
    message_id: str = Field(pattern=_GOOGLE_ID)
    body: str = Field(min_length=1, max_length=50_000)


class CalendarRespond(Strict):
    event_id: str = Field(pattern=_GOOGLE_ID)
    response: Literal["accepted", "declined", "tentative"]


# ---------------------------------------------------------------- tier 4: destructive

class GmailTrash(Strict):
    message_id: str = Field(pattern=_GOOGLE_ID)


class CalendarDelete(Strict):
    event_id: str = Field(pattern=_GOOGLE_ID)


SCHEMAS: dict[str, type[Strict]] = {
    "gmail.search": GmailSearch,
    "gmail.read": GmailRead,
    "calendar.list": CalendarList,
    "vault.read": VaultRead,
    "tasks.list": TasksList,
    "tasks.create": TasksCreate,
    "tasks.update": TasksUpdate,
    "notes.write": NotesWrite,
    "gmail.label": GmailLabel,
    "gmail.create_draft": GmailCreateDraft,
    "calendar.create_hold": CalendarCreateHold,
    "gmail.send": GmailSend,
    "gmail.reply": GmailReply,
    "calendar.respond": CalendarRespond,
    "gmail.trash": GmailTrash,
    "calendar.delete": CalendarDelete,
}

# The lowest tier each action may ever have. permissions.yaml may make an
# action stricter, never looser (a config edit can't turn "send" into tier 2).
MIN_TIER: dict[str, int] = {
    "gmail.search": 0, "gmail.read": 0, "calendar.list": 0, "vault.read": 0, "tasks.list": 0,
    "tasks.create": 1, "tasks.update": 1, "notes.write": 1,
    "gmail.label": 2, "gmail.create_draft": 2, "calendar.create_hold": 2,
    "gmail.send": 3, "gmail.reply": 3, "calendar.respond": 3,
    "gmail.trash": 4, "calendar.delete": 4,
}

# Model-facing tool names (mcp__kairo__<name>) for tiers 0-1. Tiers 2-4 have
# no tool of their own: they exist only behind propose_action.
DIRECT_TOOLS: dict[str, str] = {
    "gmail_search": "gmail.search",
    "gmail_read": "gmail.read",
    "calendar_list": "calendar.list",
    "vault_read": "vault.read",
    "tasks_list": "tasks.list",
    "tasks_create": "tasks.create",
    "tasks_update": "tasks.update",
    "notes_write": "notes.write",
}

# Fields that would let a tier-2 action reach other people. Never allowed on
# any tier-2 allow-list, whatever permissions.yaml says.
NOTIFYING_FIELDS = frozenset({
    "attendees", "guests", "sendUpdates", "sendNotifications", "conferenceData",
    "guestsCanInviteOthers", "guestsCanModify", "guestsCanSeeOtherGuests",
})


class ProposeAction(Strict):
    action: str = Field(min_length=1, max_length=100)
    args: dict = Field(default_factory=dict)
    reason: str = Field(default="", max_length=2_000)
