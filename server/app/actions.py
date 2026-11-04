"""Argument schemas for every action the model can request (KICKOFF 8).

Every schema forbids unknown fields: the gate rejects anything it was not
built to understand. An action without a schema here cannot be decided, so it
is denied.
"""

from __future__ import annotations

import re
import datetime as dt
from datetime import date, datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

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


class CapturesAnswer(Strict):
    """The owner answered a quick question in chat (a capture Kairo wasn't sure about)."""
    capture_id: int = Field(ge=1)
    answer: str = Field(min_length=1, max_length=1000)


class WishlistAdd(Strict):
    person: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    price_chf: float | None = Field(default=None, ge=0, le=100_000)
    url: str | None = Field(default=None, max_length=1000, pattern=r"^https?://\S+$")
    note: str = Field(default="", max_length=1000)


class PeopleList(Strict):
    name: str | None = Field(default=None, max_length=80)


class PeopleNote(Strict):
    name: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=2000)
    contacted: bool = False
    relation: str | None = Field(default=None, max_length=60)
    birthday: date | None = None   # YYYY-MM-DD (year unknown: 1900)
    email: str | None = Field(default=None, max_length=200)

    @field_validator("email")
    @classmethod
    def valid_email(cls, v):
        if v is not None and not _EMAIL.match(v):
            raise ValueError("not an email address")
        return v


class FinanceList(Strict):
    days: int = Field(default=30, ge=1, le=366)
    category: str | None = Field(default=None, max_length=40)


class HealthList(Strict):
    days: int = Field(default=14, ge=1, le=366)


class CoachLog(Strict):
    """v8 E: what the owner did (or skipped on purpose) for a coaching module. Kairo's own table."""

    module: Literal["training", "food", "water", "study", "faith"]
    what: str = Field(min_length=1, max_length=200)
    done: bool = True
    value: float | None = Field(default=None, ge=0, le=10_000)    # minutes, litres
    day: dt.date | None = None                                    # default: today


class CoachStatus(Strict):
    module: Literal["training", "food", "water", "study", "faith"] | None = None


class ProfileImport(Strict):
    """v8 G: the owner's own profile, shared with + (capture_id) or pasted (text), saved to the vault."""

    capture_id: int | None = Field(default=None, ge=1)
    text: str | None = Field(default=None, min_length=1, max_length=50_000)
    note: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def one_source(self):
        if (self.capture_id is None) == (self.text is None):
            raise ValueError("give capture_id (a shared file) or text (pasted), not both")
        return self


class ProfilePerson(Strict):
    name: str = Field(min_length=1, max_length=80)
    relation: str | None = Field(default=None, max_length=60)
    birthday: str | None = Field(default=None, pattern=r"^(\d{4}|-)-\d{2}-\d{2}$")   # YYYY-MM-DD, or --MM-DD without a year
    notes: str | None = Field(default=None, max_length=1000)


class ProfileLoop(Strict):
    what: str = Field(min_length=1, max_length=300)
    due: date | None = None


class CalendarRule(Strict):
    match: str = Field(min_length=1, max_length=60)        # a word in event titles; "*" = any title (needs days)
    type: Literal["FIXED", "SOCIAL", "FLEXIBLE"]
    # Owner's profile 2026-10-19 ("weekends are SOCIAL"): only events on these days. A rule with a word in
    # the title outranks a day rule (training on a Saturday stays FIXED).
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = Field(default_factory=list, max_length=7)

    @model_validator(mode="after")
    def word_or_days(self):
        if self.match.strip() == "*" and not self.days:
            raise ValueError('a rule for any title ("*") needs days')
        if self.match.strip() != "*" and len(self.match.strip()) < 2:
            raise ValueError("match is a word of at least 2 characters, or * with days")
        return self


class ProfileExam(Strict):
    subject: str = Field(min_length=1, max_length=80)
    kind: Literal["exam", "test"] = "exam"
    date: date


class ProfileApply(Strict):
    """v8 G: what the chat model read in the owner's profile. Only Kairo's own tables and the coach facts."""

    import_id: int = Field(ge=1)
    people: list[ProfilePerson] = Field(default_factory=list, max_length=30)
    loops: list[ProfileLoop] = Field(default_factory=list, max_length=30)
    calendar_rules: list[CalendarRule] = Field(default_factory=list, max_length=20)
    coach: dict = Field(default_factory=dict)
    exams: list[ProfileExam] = Field(default_factory=list, max_length=10)
    places: list["PlaceSet"] = Field(default_factory=list, max_length=8)  # home/school/work/training + stations
    questions: list[str] = Field(default_factory=list, max_length=10)   # unclear: asked next time, with a best guess

    @field_validator("questions")
    @classmethod
    def short_questions(cls, v):
        if any(not q.strip() or len(q) > 300 for q in v):
            raise ValueError("questions are 1-300 characters")
        return v

    @field_validator("coach")
    @classmethod
    def coach_facts_only(cls, v):
        from .prefs import Coach
        Coach.model_validate(v)                                # unknown keys or bad values: refused
        return v


class MemorySearch(Strict):
    """v8 C6: the owner's notes, people, open loops and conversation memory, by keywords and meaning."""

    query: str = Field(min_length=1, max_length=500)
    k: int = Field(default=5, ge=1, le=20)


class CalendarTag(Strict):
    """v7: how Kairo may treat an event. FIXED never moves (also the default for
    untagged events), SOCIAL: suggest a move + draft a message, FLEXIBLE: moved freely."""

    event_id: str = Field(pattern=_GOOGLE_ID)
    type: Literal["FIXED", "SOCIAL", "FLEXIBLE"]


class Debrief(Strict):
    """'debrief me' / 'what's going on': scope by time of day unless asked; a topic narrows it."""

    scope: Literal["auto", "today", "week", "everything"] = "auto"
    topic: Literal["calendar", "mail", "school", "tasks", "people", "money", "health", "loops"] | None = None


class SettingsList(Strict):
    pass


class SettingsSet(Strict):
    """Owner settings only (quiet hours, brief times, ping limit, ...). Never the gate or permissions."""

    key: Literal["quiet_hours", "brief_times", "max_pings", "good_times", "review_times", "language", "models"]
    value: dict | int | str


class PlaceSet(Strict):
    """'My home station is X', 'school is in Bern'. Address stays on the PC; only the station is sent."""

    name: str = Field(min_length=1, max_length=40)
    kind: Literal["home", "school", "work", "training", "other"]
    station: str | None = Field(default=None, max_length=80)
    address: str | None = Field(default=None, max_length=160)
    valid_from: date | None = None
    valid_until: date | None = None
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = Field(default_factory=list, max_length=7)   # school Mon/Tue, work Wed-Fri
    via: str | None = Field(default=None, max_length=80)              # the usual route goes through it


class PlaceRemove(Strict):
    name: str = Field(min_length=1, max_length=40)


ProfileApply.model_rebuild()                     # its places are PlaceSets (defined above)


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
    reason: Literal["done", "dropped", "not_for_me"]   # not_for_me: never a loop from this sender again


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


WEEKLY_RULE = r"^RRULE:FREQ=WEEKLY;(BYDAY=(MO|TU|WE|TH|FR|SA|SU);)?UNTIL=\d{8}(T\d{6}Z)?$"


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
    # Weekly repeat with an end within a year (school timetable). Nothing else: no daily, no count, no RDATE.
    recurrence: list[Annotated[str, Field(pattern=WEEKLY_RULE)]] | None = Field(default=None, max_length=1)
    time_zone: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def end_after_start(self):
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("start and end need a time zone")
        if self.recurrence:
            if not self.time_zone:
                raise ValueError("a repeating event needs time_zone")
            try:
                ZoneInfo(self.time_zone)
            except Exception:
                raise ValueError("unknown time_zone")
            until = re.search(r"UNTIL=(\d{8})", self.recurrence[0]).group(1)
            if dt.date(int(until[:4]), int(until[4:6]), int(until[6:])) > self.start.date() + dt.timedelta(days=366):
                raise ValueError("a repeating event must end within a year")
        return self


class GmailSendSelf(Strict):
    """Mail to the owner's own address (v7: files, study sheets, evening recap).
    There is no recipient field: the executor sends to profile.gmail, nobody else."""

    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=50_000)
    label: str | None = Field(default=None, max_length=100)
    # Files from the owner's vault (relative paths), e.g. a study sheet or OLAT material. The executor
    # reads them only from inside the vault; still only ever to the owner's own address.
    attachments: list[Annotated[str, Field(min_length=1, max_length=300)]] | None = Field(default=None, max_length=5)

    _subject = field_validator("subject")(_one_line)

    @field_validator("attachments")
    @classmethod
    def vault_paths(cls, v):
        for path in v or []:
            parts = path.replace("\\", "/").split("/")
            if (path.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", path) or ".." in parts or "" in parts
                    or any(c in path for c in "\x00\r\n")):
                raise ValueError(f"attachments are vault paths like 'Kairo/Documents/x.pdf': {path!r}")
        return v

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


class KnowledgeAskLater(Strict):
    """Something Kairo needs to know about the owner: asked on the next getting-to-know-you call."""

    question: str = Field(min_length=3, max_length=300)
    about: str | None = Field(default=None, max_length=80)


class KnowledgeAnswered(Strict):
    """A queued question got its answer (the facts themselves go through people_note / profile_learn)."""

    id: int = Field(ge=1)
    summary: str = Field(min_length=1, max_length=300)


class PendingList(Strict):
    """What waits on the owner in Kairo's own app: quick questions and getting-to-know-you questions."""


class PendingClear(Strict):
    """The owner said to clear them ("clear the get-to-know-you questions", "drop all those questions"): the ids
    given, or all open ones of that kind (optionally only those older than N days). Nothing is deleted."""

    kind: Literal["quick_question", "knowledge_question"]
    ids: list[Annotated[int, Field(ge=1)]] = Field(default_factory=list, max_length=200)
    all: bool = False
    older_than_days: int | None = Field(default=None, ge=1, le=3650)

    @model_validator(mode="after")
    def which(self):
        if not self.ids and not self.all:
            raise ValueError("give ids, or all=true")
        if self.ids and self.all:
            raise ValueError("ids or all, not both")
        return self


class PendingRestore(Strict):
    """Undo a clear: the owner wants those back."""

    kind: Literal["quick_question", "knowledge_question"]
    ids: list[Annotated[int, Field(ge=1)]] = Field(min_length=1, max_length=200)


class ProfileLearn(Strict):
    """A fact about the owner, saved to the vault (10 Profile/Learned by Kairo.md)."""

    fact: str = Field(min_length=3, max_length=500)


class DocumentCreate(Strict):
    """A document or slide deck the owner asked for: made, rendered, checked and fixed (max 3 rounds),
    then saved to the vault and mailed to the owner's own address (no recipient field)."""

    kind: Literal["document", "slides"]
    title: str = Field(min_length=1, max_length=120)
    brief: str = Field(min_length=1, max_length=4000)
    sources: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(default_factory=list, max_length=5)
    label: Literal["Kairo/School", "Kairo/Work"] | None = None

    _title = field_validator("title")(_one_line)

    @field_validator("sources")
    @classmethod
    def vault_paths(cls, v):
        return GmailSendSelf.vault_paths(v)


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
    "captures.answer": CapturesAnswer,
    "wishlist.add": WishlistAdd,
    "knowledge.ask_later": KnowledgeAskLater,
    "knowledge.answered": KnowledgeAnswered,
    "profile.learn": ProfileLearn,
    "pending.list": PendingList,
    "pending.clear": PendingClear,
    "pending.restore": PendingRestore,
    "document.create": DocumentCreate,
    "finance.list": FinanceList,
    "health.list": HealthList,
    "memory.search": MemorySearch,
    "coach.log": CoachLog,
    "profile.import": ProfileImport,
    "profile.apply": ProfileApply,
    "coach.status": CoachStatus,
    "calendar.tag": CalendarTag,
    "debrief": Debrief,
    "settings.list": SettingsList,
    "settings.set": SettingsSet,
    "place.set": PlaceSet,
    "place.remove": PlaceRemove,
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
    "shopping.list": 0, "people.list": 0, "finance.list": 0, "health.list": 0, "memory.search": 0,
    "coach.status": 0, "coach.log": 1, "profile.import": 1, "profile.apply": 1,
    "shopping.add": 1, "people.note": 1, "captures.answer": 1, "wishlist.add": 1,
    "gmail.label": 2, "gmail.create_draft": 2, "calendar.create_hold": 2,
    "gmail.send": 3, "gmail.reply": 3, "calendar.respond": 3,
    "calendar.tag": 1, "loops.list": 0, "debrief": 0, "settings.list": 0, "settings.set": 1, "place.set": 1, "place.remove": 1, "loops.add": 1, "loops.snooze": 1, "loops.close": 1,
    "knowledge.ask_later": 1, "knowledge.answered": 1, "profile.learn": 1, "document.create": 1,
    "pending.list": 0, "pending.clear": 1, "pending.restore": 1,   # Kairo's own pending items: status only
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
    "captures_answer": "captures.answer",
    "wishlist_add": "wishlist.add",
    "finance_list": "finance.list",
    "health_list": "health.list",
    "memory_search": "memory.search",
    "coach_log": "coach.log",
    "coach_status": "coach.status",
    "profile_import": "profile.import",
    "profile_apply": "profile.apply",
    "calendar_tag": "calendar.tag",
    "debrief": "debrief",
    "settings_list": "settings.list",
    "settings_set": "settings.set",
    "place_set": "place.set",
    "place_remove": "place.remove",
    "loops_list": "loops.list",
    "loops_add": "loops.add",
    "loops_snooze": "loops.snooze",
    "loops_close": "loops.close",
    "knowledge_ask_later": "knowledge.ask_later",
    "knowledge_answered": "knowledge.answered",
    "profile_learn": "profile.learn",
    "pending_list": "pending.list",
    "pending_clear": "pending.clear",
    "pending_restore": "pending.restore",
    "document_create": "document.create",
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
