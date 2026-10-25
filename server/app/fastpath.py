"""Fast paths (G10, owner's request 2026-10-24): a few questions on a call are answered by a rule on the PC, with no model.

What is answered: the time, the date and the weekday; (agenda) the next appointment, the next lesson and the next
train or bus, from Kairo's own data when it is fresh; (timers) setting, asking about and cancelling a timer or a
short reminder (timerphrase.py reads the words, timers.py rings them). Everything else goes to the voice model
exactly as before.

A wrong rule answer is worse than a slow one, so a rule only answers when it is sure:
- the whole sentence must be the question (full match, at most 10 words once the politeness and Kairo's name are
  taken off the ends). "What time is it in Tokyo", "what time is my train", "what day is the exam" are not it;
- the words and the speech-to-text language must agree, and the call's language setting is respected;
- an answer from Kairo's own data needs that data to be fresh and readable (a calendar that could not be read is not
  an empty calendar); otherwise there is no answer and the turn goes on;
- any error inside a rule is no answer, never a broken turn.

A rule's answer is a fixed template with numbers from the clock or data from the owner's own calendar, timetable and
connection. Titles from the calendar can come from other people (invites): they are cleaned to one plain line and
only read aloud, as the Today card does; nothing in them is ever run. Nothing here reaches another person, calls the
network or asks the model.

Used from voicemouth.Mouth.turn() (a rule's answer is a line Kairo says, like the greeting, with no model round) and
read once per call: `FastPaths.for_call()` reads the three switches (g10.fast_paths, g10.fast_agenda, g10.timers).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

from . import timerphrase

log = logging.getLogger("kairo.fastpath")

MAX_WORDS = 10

# ---------------------------------------------------------------- reading the sentence

# what may stand in front of / behind the question without being part of it
_LEAD = re.compile(r"^(?:(?:hey|hi|hello|hallo|okay|ok|so|also|and|und|uh|um|ah|äh|ähm|hm|kairo|please|bitte)\s+)+")
_ASK = re.compile(
    r"^(?:(?:can|could|would|will) you (?:please )?(?:just )?(?:tell|say) me|(?:please )?tell me|do you (?:happen to )?know|"
    r"kannst du mir (?:bitte )?sagen|könntest du mir (?:bitte )?sagen|kannst du (?:bitte )?sagen|sag mir(?: bitte)?|sag mal|weißt du)\s+")
_TAIL = re.compile(r"(?:\s+(?:please|bitte|kairo|thanks|thank you|danke))+$")


def _clean(text: str) -> tuple[str, bool]:
    """(the sentence without punctuation, case, politeness and the name; whether it was asked as "can you tell me ...")."""
    t = unicodedata.normalize("NFKC", text or "").lower().replace("’", "'").replace("`", "'")
    t = re.sub(r"\bist's\b", "ist es", t)
    t = re.sub(r"\b(what|who|when|where|how|which|there|that|it|here)'s\b", r"\1 is", t)
    t = re.sub(r"\bwhats\b", "what is", t)
    t = t.replace("'", "")                                     # today's -> todays, o'clock -> oclock
    t = re.sub(r"(?<=\d),(?=\d)", ".", t)                      # 1,5 hours -> 1.5 hours
    t = re.sub(r"[^\w\s.]|_", " ", t)
    t = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", t)                  # a full stop, but not the one inside 1.5
    t = re.sub(r"\s+", " ", t).strip()
    t = _TAIL.sub("", _LEAD.sub("", t))
    asked = False
    stripped = _ASK.sub("", t, count=1)
    if stripped != t:
        asked, t = True, _TAIL.sub("", _LEAD.sub("", stripped))
    return t, asked


def norm(text: str) -> str:
    return _clean(text)[0]


@dataclass
class Match:
    kind: str                                  # time / date / weekday / event / lesson / trip / timer_* (timerphrase)
    lang: str                                  # en / de: the language the words were in
    data: dict = field(default_factory=dict)


_NOW = r"(?: now| right now| currently| at the moment)?"
_JETZT = r"(?: jetzt| gerade| im moment| aktuell| eigentlich)?"
_HEUTE = r"(?: heute)?"
_TODAY = r"(?: today)?"

# kind, sub, [patterns that stand alone], [patterns that count only after "can you tell me ..."]
_RULES: dict[str, list[tuple[str, str | None, list[str], list[str]]]] = {
    "en": [
        ("time", None,
         [rf"what time is it{_NOW}", rf"what is the time{_NOW}", r"what is the current time", r"what time do you have",
          r"(?:do you have|have you got|got) the time"],
         [rf"the time{_NOW}", r"the current time", rf"what time it is{_NOW}", rf"what the time is{_NOW}"]),
        ("date", None,
         [rf"what day is it{_TODAY}", r"what day is today", rf"which day is it{_TODAY}", r"which day is today",
          rf"what is the date{_TODAY}", r"what is todays date", rf"what date is it{_TODAY}", r"which date is it today"],
         [rf"the date{_TODAY}", r"todays date", rf"what day it is{_TODAY}", r"what day today is", rf"what the date is{_TODAY}"]),
        ("weekday", None,
         [rf"what day of the week is it{_TODAY}", r"what day of the week is today"],
         [rf"what day of the week it is{_TODAY}"]),
    ],
    "de": [
        ("time", None,
         [rf"wie spät ist es{_JETZT}", rf"wie spät haben wir{_JETZT}", rf"wie ?viel uhr ist es{_JETZT}",
          rf"wie ?viel uhr haben wir{_JETZT}", rf"welche uhrzeit (?:haben wir|ist es){_JETZT}", r"wie (?:ist|lautet) die uhrzeit"],
         [rf"wie spät es{_JETZT} ist", rf"wie ?viel uhr es{_JETZT} ist", rf"die uhrzeit{_JETZT}", rf"wie spät wir{_JETZT} haben"]),
        ("date", None,
         [rf"welcher tag ist (?:heute|es{_HEUTE})", rf"welches datum haben wir{_HEUTE}", r"welches datum ist (?:heute|es(?: heute)?)",
          rf"den wievielten haben wir{_HEUTE}", r"der wievielte ist (?:heute|es(?: heute)?)", rf"welchen tag haben wir{_HEUTE}"],
         [r"welcher tag (?:heute|es) ist", r"welches datum (?:heute ist|wir heute haben|wir haben)", r"das (?:heutige )?datum(?: von heute| heute)?",
          r"den wievielten wir(?: heute)? haben", r"der wievielte (?:heute|es) ist"]),
        ("weekday", None,
         [r"welcher wochentag ist (?:heute|es(?: heute)?)", rf"welchen wochentag haben wir{_HEUTE}"],
         [r"welcher wochentag heute ist", r"welchen wochentag wir heute haben"]),
    ],
}


def _compile(rules) -> dict[str, list[tuple[str, str | None, re.Pattern, re.Pattern | None]]]:
    out: dict = {}
    for lang, rows in rules.items():
        out[lang] = []
        for kind, sub, alone, after in rows:
            out[lang].append((kind, sub, re.compile("(?:" + "|".join(alone) + ")"),
                              re.compile("(?:" + "|".join(alone + after) + ")") if after else None))
    return out


_COMPILED = _compile(_RULES)


def match(text: str, languages: tuple[str, ...] = ("en", "de")) -> Match | None:
    """The fast-path question this is, or None. Pure: no clock, no data, no side effect."""
    core, asked = _clean(text)
    words = len(core.split())
    if not core:
        return None
    if words <= MAX_WORDS:
        for lang in languages:
            for kind, _sub, alone, after in _COMPILED.get(lang, ()):
                rx = after if (asked and after is not None) else alone
                m = rx.fullmatch(core)
                if m:
                    return Match(kind, lang, {k: v for k, v in m.groupdict().items() if v})
    if words <= timerphrase.MAX_WORDS:
        got = timerphrase.parse(core, text, languages)
        if got is not None:
            return Match(got[0], got[1], got[2])
    return None


# ---------------------------------------------------------------- saying it

WEEKDAYS = {"en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
            "de": ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")}
MONTHS = {"en": ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                 "November", "December"),
          "de": ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
                 "November", "Dezember")}


def say_time(t: dt.datetime | dt.time, lang: str) -> str:
    """English: "2:35 PM", "3 PM", "noon", "midnight". German: "14 Uhr 35", "15 Uhr" (digits and the word Uhr read right in
    the German voice with or without the speech-text switch)."""
    h, m = t.hour, t.minute
    if lang == "de":
        return f"{h} Uhr" + (f" {m}" if m else "")
    if (h, m) == (12, 0):
        return "noon"
    if (h, m) == (0, 0):
        return "midnight"
    return f"{h % 12 or 12}" + (f":{m:02d}" if m else "") + (" AM" if h < 12 else " PM")


def say_date(d: dt.date, lang: str, *, year: bool = True) -> str:
    wd, mo = WEEKDAYS[lang][d.weekday()], MONTHS[lang][d.month - 1]
    if lang == "de":
        return f"{wd}, der {d.day}. {mo}" + (f" {d.year}" if year else "")
    return f"{wd}, {mo} {d.day}" + (f", {d.year}" if year else "")


@dataclass
class Answer:
    text: str
    lang: str
    kind: str


# ---------------------------------------------------------------- the rules for one call

class CallRules:
    """The rules one call uses, with the switches as they were when the call began."""

    def __init__(self, fp: "FastPaths", *, time_date: bool, agenda: bool = False, timers: bool = False):
        self.fp, self.time_date, self.agenda, self.timers = fp, time_date, agenda, timers

    def _on(self, kind: str) -> bool:
        return self.time_date if kind in ("time", "date", "weekday") else False

    def resolve(self, text: str, *, language: str | None = None, mode: str = "auto", execute: bool = True) -> Answer | None:
        """The answer a rule gives to `text`, or None (the voice model answers). language: what the speech-to-text said the
        language was; mode: the call's language setting. execute=False only asks whether a rule would answer: nothing is
        set, cancelled or counted."""
        if not (self.time_date or self.agenda or self.timers):
            return None
        langs = ("en",) if mode == "en" else ("de",) if mode == "de" else ("en", "de")
        m = match(text, langs)
        if m is None or not self._on(m.kind):
            return None
        if language in ("en", "de") and language != m.lang:
            return None                                       # the words and the speech-to-text disagree: not sure
        try:
            return self.fp.answer(m, execute=execute)
        except Exception:   # a broken rule is no answer, never a broken turn
            log.exception("fast path %s", m.kind)
            return None

    def would_answer(self, text: str, *, language: str | None = None, mode: str = "auto") -> bool:
        return self.resolve(text, language=language, mode=mode, execute=False) is not None


class FastPaths:
    """tz: the owner's zone; now: the clock (an aware datetime), injected in tests."""

    def __init__(self, *, tz, now: Callable[[], dt.datetime] | None = None, engine=None):
        self.tz, self.engine = tz, engine
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))

    def now(self) -> dt.datetime:
        return self._now().astimezone(self.tz)

    def rules(self, *, time_date: bool, agenda: bool = False, timers: bool = False) -> CallRules:
        return CallRules(self, time_date=time_date, agenda=agenda, timers=timers)

    def for_call(self) -> CallRules:
        """The rules for one call, with the owner's switches read now (at the start of the call, never per turn)."""
        from . import flags
        from .db import session_for
        try:
            with session_for(self.engine) as s:
                return self.rules(time_date=flags.on(s, "g10.fast_paths"))
        except Exception:   # no database to read: no rules, the voice model answers as before
            log.exception("fast paths: reading the switches")
            return self.rules(time_date=False)

    def answer(self, m: Match, *, execute: bool = True) -> Answer | None:
        now = self.now()
        if m.kind == "time":
            text = f"It's {say_time(now, 'en')}." if m.lang == "en" else f"Es ist {say_time(now, 'de')}."
        elif m.kind == "date":
            text = f"It's {say_date(now.date(), 'en')}." if m.lang == "en" else f"Heute ist {say_date(now.date(), 'de')}."
        elif m.kind == "weekday":
            wd = WEEKDAYS[m.lang][now.weekday()]
            text = f"It's {wd}." if m.lang == "en" else f"Heute ist {wd}."
        else:
            return None
        return Answer(text, m.lang, m.kind)
