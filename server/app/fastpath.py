"""Fast paths (G10, the owner's request in the Phase 1 voice plan): a few questions on a call are answered by a rule on the PC, with no model.

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
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

from . import timerphrase, timetable
from .timers import MAX_ACTIVE, TimerError, clean_label, say_duration, say_left

log = logging.getLogger("kairo.fastpath")

MAX_WORDS = 10
MAX_CHARS = 400              # a longer transcript is not a quick question (and is never run through the patterns)
EVENTS_MAX_AGE_S = 300.0     # the card reads the calendar every 3 minutes: older than this is not sure
TRIP_MAX_AGE_S = 240.0       # the connection every 3 minutes on a call

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
        ("event", None,
         [r"(?:what|when) is (?:my|the) next (?P<what>appointment|event|meeting)", r"what (?:do i have|have i got) next",
          r"what is next (?:on|in) my calendar"],
         [r"(?:my|the) next (?P<what>appointment|event|meeting)", r"(?:when|what) my next (?P<what>appointment|event|meeting) is"]),
        ("lesson", None,
         [r"(?:what|when) is (?:my|the) next (?:lesson|class)", r"when does (?:my|the) next (?:lesson|class) (?:start|begin)",
          r"what (?:lesson|class) do i have next", r"when do i have (?:my|the) next (?:lesson|class)", r"what (?:lesson|class) is next"],
         [r"(?:my|the) next (?:lesson|class)", r"when my next (?:lesson|class) (?:is|starts|begins)"]),
        ("trip", None,
         [r"(?:when|what) is (?:my|the) next (?P<mode>train|bus|connection)",
          r"when does (?:my|the) next (?P<mode>train|bus|connection) (?:leave|go|depart)"],
         [r"(?:my|the) next (?P<mode>train|bus|connection)", r"when (?:my|the) next (?P<mode>train|bus|connection) (?:leaves|goes|departs|is)"]),
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
        ("event", None,
         [r"(?:wann|was) ist (?:mein|der) nächste[rs]? (?P<what>termin|meeting)", r"wann habe ich (?:meinen|den) nächsten (?P<what>termin|meeting)"],
         [r"wann (?:mein|der) nächste[rs]? (?P<what>termin|meeting) ist", r"was (?:mein|der) nächste[rs]? (?P<what>termin|meeting) ist",
          r"(?:mein|meinen|der|den) nächste[rns]? (?P<what>termin|meeting)"]),
        ("lesson", None,
         [r"(?:wann|was) ist (?:meine|die) nächste (?:stunde|lektion)", r"wann habe ich (?:meine|die) nächste (?:stunde|lektion)",
          r"wann (?:beginnt|startet|fängt) (?:meine|die) nächste (?:stunde|lektion)(?: an)?",
          r"welche (?:stunde|lektion) (?:habe ich|ist|kommt) als nächstes"],
         [r"wann (?:meine|die) nächste (?:stunde|lektion) (?:ist|beginnt|startet)", r"(?:meine|die) nächste (?:stunde|lektion)"]),
        ("trip", None,
         [r"wann (?:fährt|geht|kommt) (?:der|mein) nächste[rn]? (?P<mode>zug|bus)(?: ab)?", r"wann ist (?:der|mein) nächste[rn]? (?P<mode>zug|bus)",
          r"(?:wann|was) ist (?:meine|die) nächste (?P<mode>verbindung)", r"wann (?:fährt|geht|kommt) (?:meine|die) nächste (?P<mode>verbindung)(?: ab)?",
          r"welcher (?P<mode>zug|bus) (?:fährt|kommt|geht) als nächstes"],
         [r"wann (?:der|mein) nächste[rn]? (?P<mode>zug|bus) (?:fährt|geht|kommt)(?: ab)?", r"(?:der|mein) nächste[rn]? (?P<mode>zug|bus)"]),
    ],
}


def _compile(rules) -> dict[str, list[tuple[str, list[re.Pattern], list[re.Pattern]]]]:
    """kind -> (the patterns that stand alone, the patterns that count only after "can you tell me ...")."""
    return {lang: [(kind, [re.compile(p) for p in alone], [re.compile(p) for p in after]) for kind, _sub, alone, after in rows]
            for lang, rows in rules.items()}


_COMPILED = _compile(_RULES)
_MODE = {"zug": "train", "train": "train", "bus": "bus", "verbindung": "connection", "connection": "connection"}


def match(text: str, languages: tuple[str, ...] = ("en", "de")) -> Match | None:
    """The fast-path question this is, or None. Pure: no clock, no data, no side effect."""
    if not text or len(text) > MAX_CHARS:
        return None
    core, asked = _clean(text)
    words = len(core.split())
    if not core:
        return None
    if words <= MAX_WORDS:
        for lang in languages:
            for kind, alone, after in _COMPILED.get(lang, ()):
                for rx in (alone + after) if asked else alone:
                    m = rx.fullmatch(core)
                    if m:
                        data = {k: v for k, v in m.groupdict().items() if v}
                        if "mode" in data:
                            data["mode"] = _MODE[data["mode"]]
                        return Match(kind, lang, data)
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
        if kind in ("time", "date", "weekday"):
            return self.time_date
        if kind.startswith("timer_"):
            return self.timers
        if kind in ("event", "lesson", "trip"):
            return self.agenda
        return False

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

    def __init__(self, *, tz, now: Callable[[], dt.datetime] | None = None, engine=None, timers=None, facts: "Facts | None" = None):
        self.tz, self.engine, self.timers, self.facts = tz, engine, timers, facts
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
                return self.rules(time_date=flags.on(s, "g10.fast_paths"), agenda=flags.on(s, "g10.fast_agenda"),
                                  timers=flags.on(s, "g10.timers"))
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
        elif m.kind.startswith("timer_"):
            return self._timer(m, execute)
        elif m.kind == "event":
            return self._event(m, now)
        elif m.kind == "lesson":
            return self._lesson(m, now)
        elif m.kind == "trip":
            return self._trip(m, now)
        else:
            return None
        return Answer(text, m.lang, m.kind)

    # ------------------------------------------------ the agenda: Kairo's own data, only while it is fresh and was read

    def _when(self, start: dt.datetime, now: dt.datetime, lang: str) -> str | None:
        """"at 3 PM" today, "tomorrow at 9 AM" tomorrow, None for any other day (the data does not reach that far)."""
        t = say_time(start, lang)
        if start.date() == now.date():
            return f"at {t}" if lang == "en" else f"um {t}"
        if start.date() == now.date() + dt.timedelta(days=1):
            return f"tomorrow at {t}" if lang == "en" else f"morgen um {t}"
        return None

    def _event(self, m: Match, now: dt.datetime) -> Answer | None:
        events = self.facts.events(now.date()) if self.facts is not None else None
        if events is None:
            return None
        ahead, all_day = [], False
        for e in events:
            if e.get("declined"):                              # the owner said no to it: it is not on their day
                continue
            if e.get("all_day"):
                day = _date(e.get("start"))
                all_day = all_day or (day is not None and day >= now.date())
                continue
            st = _local(e.get("start"), self.tz)
            if st is not None and st > now:                    # one that is running now is not the next one
                ahead.append((st, e))
        lang = m.lang
        if not ahead:
            return None if all_day else Answer(_AG[lang]["nothing"], lang, m.kind)
        ahead.sort(key=lambda x: x[0])
        st, e = ahead[0]
        when = self._when(st, now, lang)
        if when is None:
            return None
        title = _title(e.get("summary"), lang)
        if lang == "de":
            adj, noun = ("nächstes", "Meeting") if m.data.get("what") == "meeting" else ("nächster", "Termin")
            return Answer(f"Dein {adj} {noun} ist {title} {when}.", lang, m.kind)
        return Answer(f"Your next {m.data.get('what') or 'event'} is {title} {when}.", lang, m.kind)

    def _lesson(self, m: Match, now: dt.datetime) -> Answer | None:
        events = self.facts.events(now.date()) if self.facts is not None else None
        if events is None:
            return None
        best = None
        for e in events:
            if e.get("all_day") or e.get("declined") or e.get("summary") != timetable.SUMMARY:
                continue
            st = _local(e.get("start"), self.tz)
            if st is None:
                continue
            for les in timetable.parse_lessons(e.get("description")):
                start = dt.datetime.combine(st.date(), dt.time.fromisoformat(les["start"]), tzinfo=self.tz)
                if start > now and (best is None or start < best[0]):
                    best = (start, les)
        if best is None:                                       # school is over and the data stops here: not sure
            return None
        start, les = best
        when = self._when(start, now, m.lang)
        if when is None:
            return None
        subject, room = _title(les["subject"], m.lang), _title(les["room"], m.lang) if les["room"] else None
        if m.lang == "de":
            return Answer(f"Deine nächste Stunde ist {subject} {when}" + (f", Raum {room}" if room else "") + ".", m.lang, m.kind)
        return Answer(f"Your next lesson is {subject} {when}" + (f", in room {room}" if room else "") + ".", m.lang, m.kind)

    def _trip(self, m: Match, now: dt.datetime) -> Answer | None:
        trip = self.facts.trip() if self.facts is not None else None
        if trip is None:
            return None
        mode, lang = m.data.get("mode", "connection"), m.lang
        ups = []
        for c in trip["conns"]:
            dep = _local(c.get("departure"), self.tz)
            if dep is None:
                continue
            delay = c.get("delay") if isinstance(c.get("delay"), int) and c["delay"] > 0 else 0
            if dep + dt.timedelta(minutes=delay) > now:        # not gone yet
                ups.append((dep + dt.timedelta(minutes=delay), dep, delay, c))
        ups.sort(key=lambda x: x[0])
        cancelled = None
        for _actual, dep, delay, c in ups:
            if not _mode_ok(mode, c.get("line")):              # asked for a train and this is a bus (or not known): not sure
                return None
            if c.get("cancelled"):
                cancelled = cancelled or dep
                continue
            frm, to = _title(trip["from"], lang), _title(trip["to"], lang)
            plat = _title(c.get("platform"), lang) if c.get("platform") else None
            t = say_time(dep, lang)
            if lang == "de":
                noun = {"train": "Der nächste Zug", "bus": "Der nächste Bus", "connection": "Die nächste Verbindung"}[mode]
                text = f"{noun} nach {to} fährt um {t} in {frm} ab" + (f", Gleis {plat}" if plat else "")
                text += (f", {delay} Minute{'n' if delay != 1 else ''} Verspätung" if delay else "") + "."
                lead = f"Die Verbindung um {say_time(cancelled, lang)} fällt aus. " if cancelled else ""
            else:
                text = f"The next {mode} to {to} leaves {frm} at {t}" + (f", platform {plat}" if plat else "")
                text += (f", {delay} minute{'s' if delay != 1 else ''} late" if delay else "") + "."
                lead = f"The {say_time(cancelled, lang)} is cancelled. " if cancelled else ""
            return Answer(lead + text, lang, m.kind)
        return None

    # ------------------------------------------------ timers (timerphrase reads the words, timers.py rings them)

    def _timer(self, m: Match, execute: bool) -> Answer | None:
        t, lang, d = self.timers, m.lang, m.data
        if t is None:                                           # nothing to keep a timer with: never promise one
            return None
        if m.kind == "timer_set":
            if not execute:
                return Answer("", lang, m.kind)
            reminder = bool(d.get("reminder"))
            try:
                t.add(d["seconds"], label=d.get("label"), kind="reminder" if reminder else "timer", lang=lang)
            except TimerError as exc:
                return Answer(_TT[lang]["many" if exc.kind == "many" else "range"].format(n=MAX_ACTIVE), lang, "timer_error")
            dur = say_duration(d["seconds"], lang)
            label = clean_label(d.get("label"))
            head = _TT[lang]["remind" if reminder else "set"].format(dur=dur)
            return Answer(f"{head}: {label}." if label else f"{head}.", lang, m.kind)
        rows = t.active()
        if m.kind == "timer_list" or (m.kind == "timer_left" and not (d.get("all") or d.get("q") or d.get("seconds"))):
            return Answer(self._listing(rows, lang) if rows else _TT[lang]["none"], lang, m.kind)
        if not rows:
            return Answer(_TT[lang]["none"], lang, m.kind)
        picked = _pick(rows, d)
        if m.kind == "timer_left":
            if not picked:
                return Answer(self._nothing_like(d, lang), lang, m.kind)
            return Answer(self._listing(picked, lang), lang, m.kind)
        # cancel
        if d.get("all"):
            target = rows
        elif d.get("q") or d.get("seconds"):
            if not picked:
                return Answer(self._nothing_like(d, lang), lang, m.kind)
            target = picked
        else:
            target = rows
        if len(target) > 1 and not d.get("all"):               # which one? not guessed, nothing is cancelled
            return Answer(self._listing(target, lang) + " " + _TT[lang]["which"], lang, m.kind)
        if execute:
            t.cancel([x.id for x in target])
        word = "cancelled_all" if len(target) > 1 else "cancelled"
        return Answer(_TT[lang][word].format(n=len(target)), lang, m.kind)

    def _name(self, row, lang: str) -> str:
        if row.label:
            return row.label[0].upper() + row.label[1:]
        return _TT[lang]["unnamed"].format(dur=say_duration(row.seconds, lang))

    def _listing(self, rows: list, lang: str) -> str:
        """One timer: how long is left (with its name if it has one). Several: all of them, the first three."""
        if len(rows) == 1:
            left = say_left(self.timers.left(rows[0]), lang)
            if rows[0].label:
                return _TT[lang]["left_named"].format(name=self._name(rows[0], lang), left=left)
            return _TT[lang]["left"].format(left=left)
        parts = [_TT[lang]["left_named"].format(name=self._name(r, lang), left=say_left(self.timers.left(r), lang)) for r in rows[:3]]
        more = _TT[lang]["more"].format(k=len(rows) - 3) if len(rows) > 3 else ""
        return _TT[lang]["you_have"].format(n=len(rows)) + " " + " ".join(parts) + more

    @staticmethod
    def _nothing_like(d: dict, lang: str) -> str:
        if d.get("seconds"):
            return _TT[lang]["no_len"].format(dur=say_duration(d["seconds"], lang))
        return _TT[lang]["no_name"].format(q=d.get("q", ""))


_TT = {
    "en": {"none": "I don't have a timer running.", "range": "A timer can be from a second to a day.",
           "many": "There are already {n} timers running.", "set": "Okay, {dur}", "remind": "Okay, I'll remind you in {dur}",
           "left": "About {left} left.", "left_named": "{name}: about {left} left.", "unnamed": "The timer for {dur}",
           "you_have": "You have {n} timers.", "more": " And {k} more.",
           "which": "Say cancel all timers, or tell me which one by its name or length.",
           "cancelled": "Okay, timer cancelled.", "cancelled_all": "Okay, all {n} timers cancelled.",
           "no_name": "I don't have a timer called {q}.", "no_len": "I don't have a timer for {dur}."},
    "de": {"none": "Ich habe keinen Timer laufen.", "range": "Ein Timer geht von einer Sekunde bis zu einem Tag.",
           "many": "Es laufen schon {n} Timer.", "set": "Okay, {dur}", "remind": "Okay, ich erinnere dich in {dur}",
           "left": "Noch etwa {left}.", "left_named": "{name}: noch etwa {left}.", "unnamed": "Der Timer für {dur}",
           "you_have": "Du hast {n} Timer.", "more": " Und {k} weitere.",
           "which": "Sag alle Timer abbrechen, oder welchen, mit Namen oder Länge.",
           "cancelled": "Okay, Timer abgebrochen.", "cancelled_all": "Okay, alle {n} Timer abgebrochen.",
           "no_name": "Ich habe keinen Timer namens {q}.", "no_len": "Ich habe keinen Timer für {dur}."},
}


def _words_of(text: str) -> list[str]:
    return re.sub(r"[\W_]+", " ", (text or "").lower()).split()


def _alike(a: str, b: str) -> bool:
    """The same word, or one the plural/ending of the other (nudel / nudeln, egg / eggs), from three letters on."""
    return a == b or (min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)))


def _pick(rows: list, d: dict) -> list:
    """Which of the running timers a cancel or a question means: all, by its length, by its name, else all of them."""
    if d.get("all"):
        return rows
    if d.get("seconds"):
        return [r for r in rows if r.seconds == d["seconds"]]
    if d.get("q"):
        want = _words_of(d["q"])
        return [r for r in rows if want and all(any(_alike(w, x) for x in _words_of(r.label)) for w in want)]
    return rows


# ---------------------------------------------------------------- what the rules know of the owner's day

class Facts:
    """What the Today card last read, in a form a rule can use: the calendar's events with their times (and the timetable
    blocks' lessons), and the next connections. The card's keeper fills it (main.py) each time it reads; a rule answers from
    it only while it is fresh, was read successfully and is for today. Kept in memory, copied, never written anywhere; the
    description of an event is kept only for the school blocks (the lessons are listed in it)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock, self._lock = clock, threading.Lock()
        self._events: tuple[list[dict], bool, dt.date, float] | None = None
        self._trip: tuple[dict, float] | None = None

    def put_events(self, events: list[dict] | None, ok: bool, day: dt.date) -> None:
        keep = []
        for e in events or []:
            e = dict(e)
            if e.get("summary") != timetable.SUMMARY:
                e.pop("description", None)
            keep.append(e)
        with self._lock:
            self._events = (keep, bool(ok), day, self._clock())

    def events(self, day: dt.date) -> list[dict] | None:
        """The events read for `day`, or None: never read, the read failed, it is for another day, or it is too old."""
        with self._lock:
            cur = self._events
        if cur is None:
            return None
        events, ok, read_day, at = cur
        if not ok or read_day != day or self._clock() - at > EVENTS_MAX_AGE_S:
            return None
        return events

    def put_trip(self, conns: list[dict] | None, frm: str | None, to: str | None) -> None:
        with self._lock:
            self._trip = ({"conns": [dict(c) for c in conns], "from": frm, "to": to}, self._clock()) if conns and frm and to else None

    def trip(self) -> dict | None:
        with self._lock:
            cur = self._trip
        if cur is None or self._clock() - cur[1] > TRIP_MAX_AGE_S:
            return None
        return cur[0]


_AG = {"en": {"nothing": "Nothing else on your calendar today or tomorrow.", "untitled": "an event without a title"},
       "de": {"nothing": "Heute und morgen steht nichts mehr an.", "untitled": "ein Termin ohne Titel"}}
_MARKUP = re.compile(r"[\x00-\x1f\x7f<>\[\]{}|`\\]+")
_RAIL = {"S", "R", "RE", "IR", "IC", "ICE", "EC", "ECE", "EN", "NJ", "RJ", "RJX", "TGV", "RB", "REX", "SN", "VAE", "PE", "TER", "D", "TLK"}
_BUS = {"B", "BUS", "NFB", "NB", "BN"}


def _title(text, lang: str, limit: int = 60) -> str:
    """A name from the calendar, a lesson or a station as one plain line, read aloud and never run: no markup, no brackets,
    at most `limit` characters (cut at a word)."""
    raw = str(text or "")
    if raw.strip() in ("", "(no title)"):
        return _AG[lang]["untitled"]
    t = re.sub(r"\s+", " ", _MARKUP.sub(" ", raw)).strip()
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0] if " " in t[:limit] else t[:limit]
    return t.strip(" ,;:") or _AG[lang]["untitled"]


def _mode_ok(mode: str, line) -> bool:
    """Is this connection the kind of vehicle that was asked for ("S3" and "IC8" are trains, "B12" a bus)? "connection" takes any."""
    if mode == "connection":
        return True
    m = re.match(r"^([A-Za-z]+)", str(line or "").strip())
    cat = m.group(1).upper() if m else ""
    return cat in (_RAIL if mode == "train" else _BUS)


def _local(t, tz) -> dt.datetime | None:
    if isinstance(t, dt.datetime):
        pass
    elif isinstance(t, str):
        try:
            t = dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).astimezone(tz)


def _date(t) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(t)[:10])
    except ValueError:
        return None
