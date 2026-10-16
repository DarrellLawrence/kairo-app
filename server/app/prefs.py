"""Owner settings, changed by voice or in More -> Settings instead of editing config files.

Stored in Kairo's own database (settings table, key "prefs"), applied on top of
config/schedule.yaml and config/profile.yaml at start and whenever they change.
Only the settings listed here exist; the gate, permissions and secrets are never
settings.

Places: where the owner lives, studies, works and trains. Only the station name
is ever sent anywhere (to the public transport and weather APIs); an address, if
given, stays on this PC and is only used to recognise calendar locations.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlmodel import Session

from .db import SettingKV

KEY = "prefs"
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
PLACE_KINDS = ("home", "school", "work", "training", "other")


class PrefsError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _hhmm(v: str) -> str:
    if not isinstance(v, str) or not HHMM.match(v):
        raise ValueError("time must be HH:MM, e.g. 06:20")
    return v


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class Place(_Strict):
    name: str = Field(min_length=1, max_length=40)
    kind: Literal["home", "school", "work", "training", "other"]
    station: str | None = Field(default=None, max_length=80)     # sent to the transport/weather APIs
    address: str | None = Field(default=None, max_length=160)    # private: stays on the PC
    valid_from: dt.date | None = None
    valid_until: dt.date | None = None
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = Field(default_factory=list, max_length=7)   # the weekdays it's on (empty: every day)
    via: str | None = Field(default=None, max_length=80)              # the route goes through this station

    def active(self, day: dt.date) -> bool:
        return (self.valid_from is None or self.valid_from <= day) and (self.valid_until is None or day <= self.valid_until)

    def on(self, day: dt.date) -> bool:
        """Active, and one of its weekdays (school Mon/Tue, work Wed-Fri)."""
        return self.active(day) and (not self.days or WEEKDAYS[day.weekday()] in self.days)


class QuietHours(_Strict):
    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def t(cls, v):
        return _hhmm(v)


class BriefTimes(_Strict):
    weekdays: str = "06:20"
    weekends: str = "08:20"

    @field_validator("weekdays", "weekends")
    @classmethod
    def t(cls, v):
        return _hhmm(v)


class GoodTimes(_Strict):
    after_work: str | None = None
    weekend: str | None = None
    morning: str | None = None
    morning_weekend: str | None = None

    @field_validator("after_work", "weekend", "morning", "morning_weekend")
    @classmethod
    def t(cls, v):
        return None if v is None else _hhmm(v)


class ReviewTimes(_Strict):
    daily: str | None = None
    big: str | None = None

    @field_validator("daily", "big")
    @classmethod
    def t(cls, v):
        return None if v is None else _hhmm(v)


class CallWindow(_Strict):
    name: str = Field(min_length=1, max_length=40)
    days: Literal["weekdays", "weekend", "all"] = "all"
    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def t(cls, v):
        return _hhmm(v)


DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class SavingsGoal(_Strict):
    name: str = Field(min_length=1, max_length=60)
    chf: float = Field(gt=0, le=1_000_000)
    by: dt.date | None = None


class Coach(_Strict):
    """v8 E: the owner's coaching facts. Generic features; the values come from the owner (chat, the profile import)."""

    training_days: list[Day] = Field(default_factory=list, max_length=7)       # fixed training: never planned over
    work_days: list[Day] = Field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"], max_length=7)
    work_hours: str = "08:00-17:00"                                            # school or work
    home_sessions: int = Field(default=2, ge=0, le=6)                          # short home sessions a week
    home_minutes: int = Field(default=20, ge=5, le=90)
    equipment: list[str] = Field(default_factory=list, max_length=20)          # home sessions use only these
    water_l: float | None = Field(default=None, ge=0.5, le=6)                  # daily goal; no goal = no reminders
    food_check: str | None = "20:00"                                           # evening-before check; None = off
    study_sessions: int = Field(default=3, ge=1, le=7)                         # a week, until an exam
    faith_habit: str | None = Field(default=None, max_length=120)              # small, daily
    faith_after: str | None = Field(default=None, max_length=80)               # next to this existing morning habit
    snacks_week_chf: float | None = Field(default=None, ge=0, le=10_000)
    gifts_year_chf: float | None = Field(default=None, ge=0, le=100_000)
    style_month_chf: float | None = Field(default=None, ge=0, le=10_000)
    savings: list[SavingsGoal] = Field(default_factory=list, max_length=10)

    @field_validator("equipment")
    @classmethod
    def items(cls, v):
        out: dict[str, str] = {}
        for x in v:
            name = str(x).strip()[:40]
            if name:
                out.setdefault(name.lower(), name)
        return list(out.values())

    @field_validator("work_hours")
    @classmethod
    def hours(cls, v):
        a, sep, b = str(v).partition("-")
        if not sep or _hhmm(a.strip()) >= _hhmm(b.strip()):
            raise ValueError("work_hours like 08:00-17:00")
        return f"{a.strip()}-{b.strip()}"

    @field_validator("food_check")
    @classmethod
    def t(cls, v):
        return None if v is None else _hhmm(v)


def coach(s: Session) -> Coach:
    return Coach.model_validate(load(s).get("coach") or {})


SETTINGS = {
    # key: (validator, what it means)
    "quiet_hours": (QuietHours, "No pushes and no background work between these times."),
    "brief_times": (BriefTimes, "When the morning brief comes (weekdays / weekends)."),
    "max_pings": (int, "Most proactive pings a day (1-20). Replies from real people always come."),
    "good_times": (GoodTimes, "When 'later' things come back: after_work, weekend, morning, morning_weekend."),
    "review_times": (ReviewTimes, "Evening check-in (daily) and Sunday review (big) times."),
    "language": (str, "Default language: English or German."),
    "models": (dict, "Model per job, e.g. {\"brief\": \"sonnet\"} (haiku / sonnet / opus). Judgment never Haiku, money stays Opus."),
    "call_windows": (list, "When Kairo may ring (important things and real questions only): a list of "
                           "{name, days: weekdays|weekend|all, start, end}, e.g. morning commute 06:00-08:00."),
    "coach": (Coach, "Coaching facts (only the fields given change): training_days and work_days (mon..sun), "
                     "work_hours 08:00-17:00, home_sessions a week, home_minutes, equipment (home sessions use only "
                     "these), water_l (daily goal), food_check (time, or null), study_sessions a week, faith_habit + "
                     "faith_after (an existing morning habit), snacks_week_chf, gifts_year_chf, style_month_chf, "
                     "savings [{name, chf, by}]."),
    "recap": (str, "Evening recap mail to your own Gmail: a time like 20:30, or off (the default)."),
}


def load(s: Session) -> dict:
    row = s.get(SettingKV, KEY)
    return json.loads(row.value) if row and row.value else {}


def _save(s: Session, data: dict) -> dict:
    row = s.get(SettingKV, KEY) or SettingKV(key=KEY, value="{}")
    row.value = json.dumps(data, default=str)
    s.add(row)
    s.commit()
    return data


def validate(key: str, value):
    if key not in SETTINGS:
        raise PrefsError(f"no setting {key!r}; there are: {', '.join(SETTINGS)}")
    kind = SETTINGS[key][0]
    try:
        if kind is int:
            v = int(value)
            if not 1 <= v <= 20:
                raise ValueError("between 1 and 20")
            return v
        if key == "language":
            v = {"en": "English", "english": "English", "de": "German", "german": "German", "deutsch": "German"}.get(str(value).lower())
            if not v:
                raise ValueError("English or German")
            return v
        if key == "recap":
            v = str(value).strip().lower()
            return "off" if v in ("off", "no", "none", "aus") else _hhmm(v)
        if key == "models":
            if not isinstance(value, dict) or any(v not in ("haiku", "sonnet", "opus") for v in value.values()):
                raise ValueError("models are haiku, sonnet or opus")
            return {str(k)[:40]: v for k, v in value.items()}
        if key == "call_windows":
            if not isinstance(value, list) or not 1 <= len(value) <= 6:
                raise ValueError("a list of 1-6 windows")
            out = [CallWindow.model_validate(w).model_dump(mode="json") for w in value]
            if any(w["start"] >= w["end"] for w in out):
                raise ValueError("each window starts before it ends (no windows over midnight)")
            return out
        if key == "coach":
            return kind.model_validate(value).model_dump(mode="json")   # null switches a coach item off
        return kind.model_validate(value).model_dump(mode="json", exclude_none=True)
    except (ValidationError, ValueError, TypeError) as exc:
        msg = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
        raise PrefsError(f"{key}: {msg}")


def set_key(s: Session, key: str, value) -> dict:
    data = load(s)
    if key == "coach" and isinstance(value, dict):        # one fact at a time: the rest stays
        value = {**(data.get("coach") or {}), **value}
    data[key] = validate(key, value)
    return _save(s, data)


def places(s: Session) -> list[Place]:
    return [Place.model_validate(p) for p in load(s).get("places", [])]


def place_set(s: Session, place: dict) -> Place:
    try:
        p = Place.model_validate(place)
    except ValidationError as exc:
        raise PrefsError(exc.errors()[0]["msg"])
    data = load(s)
    rest = [x for x in data.get("places", []) if x["name"].lower() != p.name.lower()]
    data["places"] = rest + [p.model_dump(mode="json", exclude_none=True)]
    _save(s, data)
    return p


def place_remove(s: Session, name: str) -> bool:
    data = load(s)
    before = data.get("places", [])
    data["places"] = [x for x in before if x["name"].lower() != name.lower()]
    _save(s, data)
    return len(before) != len(data["places"])


def active_place(s: Session, kind: str, day: dt.date) -> Place | None:
    for p in places(s):
        if p.kind == kind and p.active(day):
            return p
    return None


def public_view(s: Session) -> dict:
    """For the model and the app. Addresses are shown to the owner in the app only."""
    data = load(s)
    return {"settings": {k: data.get(k) for k in SETTINGS}, "about": {k: v[1] for k, v in SETTINGS.items()},
            "places": data.get("places", [])}


def apply(prefs: dict, *, schedule: dict, profile) -> None:
    """Lay the owner's settings over config (in place, so every holder of these objects sees them)."""
    if prefs.get("quiet_hours"):
        profile.quiet_hours.start = prefs["quiet_hours"]["start"]      # the one source (jobs and pushes)
        profile.quiet_hours.end = prefs["quiet_hours"]["end"]
    if prefs.get("language"):
        profile.default_language = prefs["language"]
    if prefs.get("max_pings"):
        schedule.setdefault("proactive", {})["max_per_day"] = prefs["max_pings"]
    if prefs.get("good_times"):
        schedule.setdefault("loops", {}).setdefault("times", {}).update(prefs["good_times"])
    if prefs.get("review_times"):
        schedule.setdefault("reviews", {}).update(prefs["review_times"])
    if prefs.get("models"):
        schedule.setdefault("models", {}).update(prefs["models"])
    if prefs.get("recap") and prefs["recap"] != "off":
        h, m = prefs["recap"].split(":")
        schedule.setdefault("jobs", {}).setdefault("evening_recap", {})["cron"] = f"{int(m)} {int(h)} * * *"
    else:   # off (the default): not planned at all
        ((schedule.get("jobs") or {}).get("evening_recap") or {}).pop("cron", None)
    if prefs.get("brief_times"):
        mb = schedule.setdefault("jobs", {}).setdefault("morning_brief", {})
        for k, cron_days in (("weekdays", "1-5"), ("weekends", "6,0")):
            if prefs["brief_times"].get(k):
                h, m = prefs["brief_times"][k].split(":")
                mb[k] = f"{int(m)} {int(h)} * * {cron_days}"
