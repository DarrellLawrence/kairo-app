"""Argument schemas for every action the model can request (KICKOFF 8).

Every schema forbids unknown fields: the gate rejects anything it was not
built to understand. An action without a schema here cannot be decided, so it
is denied.
"""

from __future__ import annotations

import re
from datetime import date, datetime
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
    status: Literal["open", "doing", "done", "dropped"] | None = "open"


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
    status: Literal["open", "doing", "done", "dropped"] | None = None
    place: str | None = Field(default=None, pattern=_PLACE)


class NotesWrite(Strict):
    path: str = Field(min_length=1, max_length=300)  # inside the vault's Kairo-owned folder
    content: str = Field(max_length=100_000)
    mode: Literal["overwrite", "append"] = "overwrite"


# ---------------------------------------------------------------- phase 3: life data (Kairo's own tables)

class ShoppingList(Strict):
    pass


class ShoppingAdd(Strict):
    items: list[str] = Field(min_length=1, max_length=30)

    @field_validator("items")
    @classmethod
    def short_lines(cls, v: list[str]) -> list[str]:
        for item in v:
            if not item.strip() or len(item) > 200 or "\n" in item:
                raise ValueError("each item: one line, 1-200 characters")
        return [i.strip() for i in v]


class PeopleList(Strict):
    name: str | None = Field(default=None, max_length=80)


class PeopleNote(Strict):
    name: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=2000)
    contacted: bool = False
    relation: str | None = Field(default=None, max_length=60)


class FinanceList(Strict):
    days: int = Field(default=30, ge=1, le=366)
    category: str | None = Field(default=None, max_length=40)


class HealthList(Strict):
    days: int = Field(default=14, ge=1, le=366)


class CalendarTag(Strict):
    """v7: how Kairo may treat an event. FIXED never moves (also the default for
    untagged events), SOCIAL: suggest a move + draft a message, FLEXIBLE: moved freely."""

    event_id: str = Field(pattern=_GOOGLE_ID)
    type: Literal["FIXED", "SOCIAL", "FLEXIBLE"]


class Debrief(Strict):
    """'debrief me' / 'what's going on': scope by time of day unless asked; a topic narrows it."""

    scope: Literal["auto", "today", "week", "everything"] = "auto"
    topic: Literal["calendar", "mail", "school", "tasks", "people", "money", "health", "loops"] | None = None


class LoopsList(Strict):
    include_snoozed: bool = True


class LoopsAdd(Strict):
    """Something to keep raising: a promise the owner made, something someone waits for."""

    kind: Literal["promise", "waiting", "task", "deadline"]
    who: str | None = Field(default=None, max_length=120)
    what: str = Field(min_length=1, max_length=300)
    due: date | None = None

    _what = field_validator("what")(_one_line)


class LoopsSnooze(Strict):
    """'not now' = later (by urgency), 'in an hour', 'tomorrow', or an exact time."""

    id: int = Field(ge=1)
    when: Literal["later", "in_1h", "tomorrow"] | None = None
    at: datetime | None = None

    @model_validator(mode="after")
    def one_of(self):
        if (self.when is None) == (self.at is None):
            raise ValueError("give when or at")
        if self.at is not None and self.at.tzinfo is None:
            raise ValueError("at needs a time zone")
        return self


class LoopsClose(Strict):
    id: int = Field(ge=1)
    reason: Literal["done", "dropped"]


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


class GmailSendSelf(Strict):
    """Mail to the owner's own address (v7: files, study sheets, evening recap).
    There is no recipient field: the executor sends to profile.gmail, nobody else."""

    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=50_000)
    label: str | None = Field(default=None, max_length=100)

    _subject = field_validator("subject")(_one_line)

    @field_validator("label")
    @classmethod
    def kairo_label(cls, v):
        if v is not None and not v.startswith(KAIRO_LABEL_PREFIX):
            raise ValueError(f"label must be one of Kairo's own ({KAIRO_LABEL_PREFIX}...)")
        return v


class CalendarMove(Strict):
    """New times for one of the owner's own events. Only times: no guests, no
    notifications. The executor also refuses events with guests or not FLEXIBLE."""

    event_id: str = Field(pattern=_GOOGLE_ID)
    start: datetime
    end: datetime

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
    "shopping.list": ShoppingList,
    "shopping.add": ShoppingAdd,
    "people.list": PeopleList,
    "people.note": PeopleNote,
    "finance.list": FinanceList,
    "health.list": HealthList,
    "calendar.tag": CalendarTag,
    "debrief": Debrief,
    "loops.list": LoopsList,
    "loops.add": LoopsAdd,
    "loops.snooze": LoopsSnooze,
    "loops.close": LoopsClose,
    "gmail.send_self": GmailSendSelf,
    "calendar.move": CalendarMove,
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
    "shopping.list": 0, "people.list": 0, "finance.list": 0, "health.list": 0,
    "shopping.add": 1, "people.note": 1,
    "gmail.label": 2, "gmail.create_draft": 2, "calendar.create_hold": 2,
    "gmail.send": 3, "gmail.reply": 3, "calendar.respond": 3,
    "calendar.tag": 1, "loops.list": 0, "debrief": 0, "loops.add": 1, "loops.snooze": 1, "loops.close": 1,
    "gmail.send_self": 2, "calendar.move": 2,
    "gmail.trash": 2,   # v7: Gmail keeps trash 30 days
    "calendar.delete": 4,
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
    "shopping_list": "shopping.list",
    "shopping_add": "shopping.add",
    "people_list": "people.list",
    "people_note": "people.note",
    "finance_list": "finance.list",
    "health_list": "health.list",
    "calendar_tag": "calendar.tag",
    "debrief": "debrief",
    "loops_list": "loops.list",
    "loops_add": "loops.add",
    "loops_snooze": "loops.snooze",
    "loops_close": "loops.close",
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
