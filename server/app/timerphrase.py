"""Reading a timer sentence, English and German (G10; used by fastpath.match). Pure: words in, a small record out.

A sentence is read only when all of it is one of these:
- set:    "set a timer for 10 minutes [for the pasta]", "timer for 5 minutes", "set a 20 minute timer",
          "remind me in 20 minutes [to call Lea]"; German: "Stell einen Timer auf 10 Minuten [für die Nudeln]",
          "Timer auf 5 Minuten", "Erinner mich in 20 Minuten [an die Wäsche]";
- ask:    how long is left on the timer / which timers there are;
- cancel: the timer, all timers, the 10 minute timer, the pasta timer.
The length must be clear: a number (digits or words) with a unit (seconds, minutes, hours), "half an hour", "a quarter of an
hour", "an hour and a half", "anderthalb Stunden", "eine Viertelstunde" and sums of those. Anything else is no match and
the voice model gets the sentence: no length ("set a timer for the pasta"), a time of day ("remind me at five"), a day
("in two days"), words left over that are not a label ("... then call Lea"), a label with a time in it. The label is the
owner's own words, at most 8 of them. It is cleaned again before it is stored (timers.clean_label).
"""

from __future__ import annotations

import re

MAX_WORDS = 20            # a longer sentence is not a timer sentence
MAX_LABEL_WORDS = 8

_EN_ONES = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
            "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
            "seventeen": 17, "eighteen": 18, "nineteen": 19}
_EN_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_DE_ONES = {"ein": 1, "eine": 1, "einen": 1, "einem": 1, "einer": 1, "eins": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5,
            "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "elf": 11, "zwölf": 12, "dreizehn": 13, "vierzehn": 14,
            "fünfzehn": 15, "sechzehn": 16, "siebzehn": 17, "achtzehn": 18, "neunzehn": 19}
_DE_TENS = {"zwanzig": 20, "dreißig": 30, "dreissig": 30, "vierzig": 40, "fünfzig": 50, "sechzig": 60, "siebzig": 70,
            "achtzig": 80, "neunzig": 90}
_DE_UND = re.compile(r"^(ein|zwei|drei|vier|fünf|sechs|sieben|acht|neun)und(zwanzig|dreißig|dreissig|vierzig|fünfzig|sechzig|"
                     r"siebzig|achtzig|neunzig)$")
_DE_HALF_PLUS = {"anderthalb": 1.5, "eineinhalb": 1.5, "zweieinhalb": 2.5, "dreieinhalb": 3.5, "viereinhalb": 4.5, "fünfeinhalb": 5.5}

_UNITS = {
    "en": {"second": 1, "seconds": 1, "sec": 1, "secs": 1, "minute": 60, "minutes": 60, "min": 60, "mins": 60,
           "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600},
    "de": {"sekunde": 1, "sekunden": 1, "sek": 1, "minute": 60, "minuten": 60, "min": 60, "stunde": 3600, "stunden": 3600, "std": 3600},
}
_NUM = re.compile(r"\d+(?:\.\d+)?")


def _number(tokens: list[str], i: int, lang: str) -> tuple[float, int] | None:
    """(value, tokens used) of a number at tokens[i]: 10, 1.5, ten, twenty five, fünfundzwanzig."""
    if i >= len(tokens):
        return None
    w = tokens[i]
    if _NUM.fullmatch(w):
        return float(w), 1
    if lang == "en":
        if w in _EN_TENS:
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if nxt in _EN_ONES and nxt not in ("a", "an"):              # twenty five, forty one
                return float(_EN_TENS[w] + _EN_ONES[nxt]), 2
            return float(_EN_TENS[w]), 1
        if w in _EN_ONES:
            return float(_EN_ONES[w]), 1
        return None
    if w in _DE_ONES:
        return float(_DE_ONES[w]), 1
    if w in _DE_TENS:
        return float(_DE_TENS[w]), 1
    m = _DE_UND.match(w)
    if m:
        return float(_DE_ONES[m.group(1)] + _DE_TENS[m.group(2)]), 1
    return None


def _atom(tokens: list[str], i: int, lang: str) -> tuple[float, int] | None:
    """(seconds, tokens used) of one length at tokens[i]: "10 minutes", "half an hour", "a quarter of an hour"."""
    units, n = _UNITS[lang], len(tokens)
    t = lambda k: tokens[i + k] if i + k < n else ""  # noqa: E731
    if lang == "en":
        if t(0) == "half":                                                  # half an hour, half a minute, half hour
            k = 1 + (t(1) in ("an", "a"))
            if t(k) in units:
                return 0.5 * units[t(k)], k + 1
            return None
        if (t(0) in ("a", "one", "three") and t(1) in ("quarter", "quarters")) or t(0) == "quarter":
            k = (1 if t(0) == "quarter" else 2)
            q = 3 if t(0) == "three" else 1
            if t(k) == "of":
                k += 1
            if t(k) in ("an", "a"):
                k += 1
            if t(k) == "hour":
                return q * 900.0, k + 1
            return None
        num = _number(tokens, i, "en")
        if num is None:
            return None
        value, used = num
        k = used
        if t(k) == "and" and t(k + 1) == "a" and t(k + 2) == "half" and t(k + 3) in units:      # one and a half hours
            return (value + 0.5) * units[t(k + 3)], k + 4
        if t(k) in units:
            secs = value * units[t(k)]
            if t(k + 1) == "and" and t(k + 2) == "a" and t(k + 3) == "half":                     # an hour and a half
                secs += 0.5 * units[t(k)]
                return secs, k + 4
            return secs, k + 1
        return None
    # German
    if t(0) in _DE_HALF_PLUS and t(1) in units:
        return _DE_HALF_PLUS[t(0)] * units[t(1)], 2
    k = 1 if t(0) in ("eine", "einer", "einen", "ne", "einem") else 0
    if t(k) in ("halbe", "halben", "halber", "halbes") and t(k + 1) in units:
        return 0.5 * units[t(k + 1)], k + 2
    if t(k) == "viertelstunde":
        return 900.0, k + 1
    if t(k) == "dreiviertelstunde":
        return 2700.0, k + 1
    num = _number(tokens, i, "de")
    if num is not None and t(num[1]) in units:
        return num[0] * units[t(num[1])], num[1] + 1
    return None


def parse_duration(tokens: list[str], lang: str, start: int = 0) -> tuple[int | None, int]:
    """(whole seconds, index after the length) of the longest length at tokens[start:], else (None, start)."""
    total, i, got = 0.0, start, False
    while True:
        a = _atom(tokens, i, lang)
        if a is None:
            break
        total, i, got = total + a[0], i + a[1], True
        if i + 1 < len(tokens) and tokens[i] in ("and", "und") and _atom(tokens, i + 1, lang) is not None:
            i += 1
    if not got:
        return None, start
    secs = round(total)
    return (secs if secs >= 1 and abs(total - secs) < 0.5 else None), i


# ---------------------------------------------------------------- labels

_LABEL_JOIN = {"en": {"timer": ("for", "called", "named", "labeled", "labelled", "to", "about"),
                      "reminder": ("to", "about", "that", "of", "for")},
               "de": {"timer": ("für", "namens", "genannt", "wegen", "zum", "zur", "fürs"),
                      "reminder": ("an", "daran", "wegen", "dass", "zum", "zur", "für")}}
_KEEP_JOINER = {"an"}                                          # "Erinnerung: an die Wäsche": the word belongs to the label
_WHEN = re.compile(
    r"\b(?:tomorrow|tonight|yesterday|today|morning|afternoon|evening|noon|midnight|oclock|uhr|morgen|heute|gestern|abend|abends|"
    r"nachmittag|vormittag|mittag|mitternacht|monday|tuesday|wednesday|thursday|friday|saturday|sunday|montag|dienstag|mittwoch|"
    r"donnerstag|freitag|samstag|sonntag|until|till|bis)\b|\b\d{1,2}(?:\.\d+)? ?(?:am|pm|h)\b|\b(?:at|um|by|on|am) \d", re.I)


# two requests in one sentence ("... for the pasta and also one for the eggs"): not a label, the voice model reads it whole
_COMPOUND = re.compile(r"\b(?:and|then|also|or|but|und|dann|auch|oder|aber|danach|außerdem)\b")


def _label(tokens: list[str], i: int, lang: str, kind: str, original: str) -> tuple[bool, str | None]:
    """(ok, label) for what is left after the length. Nothing left is fine; words that do not start a label are not."""
    rest = tokens[i:]
    if not rest:
        return True, None
    if rest[0] not in _LABEL_JOIN[lang][kind]:
        return False, None
    words = rest if rest[0] in _KEEP_JOINER else rest[1:]
    if not words or len(words) > MAX_LABEL_WORDS or _WHEN.search(" ".join(rest)) or _COMPOUND.search(" ".join(words)):
        return False, None
    label = " ".join(words)
    if parse_duration(words, lang)[0] is not None:                  # "for 5 minutes to 10 minutes": not a label
        return False, None
    m = re.search(re.escape(label), original, re.I)                  # the owner's own capital letters, if they survive
    if m is None:                                                      # "o'clock", "1,5": punctuation was taken out
        flat = re.sub(r"[\W_]+", " ", original)
        m = re.search(re.escape(label), flat, re.I)
        return True, (flat[m.start():m.end()] if m else label)
    return True, original[m.start():m.end()]


# ---------------------------------------------------------------- the sentences

_P = {
    "en": re.compile(r"^(?:(?:can|could|would|will) you (?:please )?|i (?:want|need|would like|ll need) (?:you to |to )?|i ?d like (?:you to |to )?|"
                     r"let s |go ahead and |just )+"),
    "de": re.compile(r"^(?:(?:kannst|könntest|würdest|magst) du (?:mir )?(?:bitte )?(?:mal )?|ich (?:möchte|will|brauche|hätte gern|hätte gerne|bräuchte) |"
                     r"mach mal |bitte )+"),
}
_VERB = {"en": r"(?:set|start|create|make|put|begin|run)(?: me| up)?",
         "de": r"(?:stell|stelle|stellt|starte|start|setz|setze|mach|mache|erstelle|aktiviere)(?: mir| mal| bitte| doch)*"}
_ART = {"en": r"(?:(?:a|an|the|one|another|new) )*", "de": r"(?:(?:einen|ein|den|nen|noch einen|so einen|neuen|einen neuen) )?"}

_SET = {
    "en": [
        ("timer", re.compile(rf"^{_VERB['en']} {_ART['en']}timer(?: for| of| on| to| at)? (?P<rest>.+)$")),
        ("timer", re.compile(rf"^{_ART['en']}timer(?: for| of| on)? (?P<rest>.+)$")),
        ("timer", re.compile(rf"^(?:{_VERB['en']} )?{_ART['en']}(?P<rest>[a-z0-9 ]+?) timer(?P<tail>(?: .+)?)$")),
        ("reminder", re.compile(r"^remind me (?:in|after) (?P<rest>.+)$")),
        ("reminder", re.compile(r"^set (?:a|an) (?:reminder|alarm)(?: for| in)? (?P<rest>.+)$")),
    ],
    "de": [
        ("timer", re.compile(rf"^{_VERB['de']} {_ART['de']}timer(?: auf| für| von| mit)? (?P<rest>.+?)(?: (?:stellen|starten|machen|setzen))?$")),
        ("timer", re.compile(rf"^{_ART['de']}timer(?: auf| für| von| mit)? (?P<rest>.+?)(?: (?:stellen|starten|machen|setzen))?$")),
        ("timer", re.compile(rf"^(?:{_VERB['de']} )?{_ART['de']}(?P<rest>[a-zäöüß0-9 ]+?) timer(?P<tail>(?: .+)?)$")),
        ("reminder", re.compile(r"^(?:erinner|erinnere)(?: mich| mal| bitte)* (?:in|nach) (?P<rest>.+)$")),
        ("reminder", re.compile(r"^mich(?: bitte)? (?:in|nach) (?P<rest>.+?) (?:zu )?erinnern$")),
        ("reminder", re.compile(r"^(?:stell|stelle)(?: mir)? (?:eine|ne) erinnerung (?:in|auf|für|nach) (?P<rest>.+)$")),
    ],
}

_EN_T = (r"(?:(?P<art>my|the|that|this|a|an|all my|all the|all|every) )?(?:(?P<q>[a-z0-9 ]{1,40}?) )?(?P<noun>timers?)")
_DE_T = (r"(?:(?P<art>den|dem|der|die|das|mein|meinen|meinem|meine|meiner|diesen|diesem|diese|alle meine|alle|jeden|jedem) )?(?:(?P<q>[a-zäöüß0-9 ]{1,40}?) )?(?P<noun>timer)")

_ASK = {
    "en": [
        ("timer_left", re.compile(rf"^how (?:much time|long|much)(?: is| do i have| does it have| does my timer have| does the timer have)? (?:left|remaining)(?: on| for| in| of)? {_EN_T}$")),
        ("timer_left", re.compile(rf"^how (?:much time|long|much) (?:is )?(?:left|remaining) on {_EN_T}$")),
        ("timer_left", re.compile(rf"^how much longer (?:on|for|is|does) {_EN_T}(?: have)?$")),
        ("timer_left", re.compile(rf"^how (?:much time|long) does {_EN_T} have(?: left)?$")),
        ("timer_left", re.compile(rf"^how long (?:until|till|before) {_EN_T} (?:is up|rings|goes off|ends|is done|finishes|expires)$")),
        ("timer_left", re.compile(rf"^when (?:does|will|is) {_EN_T} (?:go off|ring|end|finish|be done|be up|up|expire)$")),
        ("timer_left", re.compile(rf"^(?:what|how much) is left on {_EN_T}$")),
        ("timer_list", re.compile(r"^(?:which|what) timers (?:do i have|have i got|are running|are set|are there|do i have running)(?: running| set)?(?: right now| now| at the moment)?$")),
        ("timer_list", re.compile(r"^(?:list|show|tell me)(?: me)?(?: all)?(?: my| the)? timers$")),
        ("timer_list", re.compile(r"^(?:do i have|have i got) (?:a|any) timers?(?: running| set| going| active)?(?: right now| now| at the moment)?$")),
        ("timer_list", re.compile(r"^(?:is|are) (?:there )?(?:a|any) timers? (?:running|set|going|active)$")),
        ("timer_list", re.compile(r"^how many timers (?:do i have|are running|are there|have i got)$")),
        ("timer_cancel", re.compile(rf"^(?:cancel|stop|delete|remove|clear|turn off|end|kill|dismiss|abort|shut off)(?: off)? {_EN_T}$")),
        ("timer_cancel", re.compile(rf"^(?:cancel|stop|delete|remove|clear) {_EN_T} (?:for|called|named) (?P<q2>[a-z0-9 ]{{1,40}})$")),
    ],
    "de": [
        ("timer_left", re.compile(rf"^wie lange (?:läuft|dauert) {_DE_T} noch$")),
        ("timer_left", re.compile(rf"^wie ?viel zeit (?:ist|bleibt|habe ich)(?: noch)? (?:noch )?(?:auf|bei|an) {_DE_T}$")),
        ("timer_left", re.compile(rf"^wie lange noch bis {_DE_T} (?:klingelt|fertig ist|abläuft|abgelaufen ist|zu ende ist)$")),
        ("timer_left", re.compile(rf"^wann ist {_DE_T} (?:fertig|abgelaufen|vorbei|zu ende)$")),
        ("timer_left", re.compile(rf"^wann (?:klingelt|läutet|geht|endet) {_DE_T}(?: los| ab)?$")),
        ("timer_list", re.compile(r"^welche timer (?:habe ich|laufen|sind gestellt|gibt es)(?: gerade| noch| zurzeit| im moment)?$")),
        ("timer_list", re.compile(r"^(?:habe ich|hab ich) (?:einen|irgendeinen|noch einen) timer (?:laufen|gestellt|am laufen)$")),
        ("timer_list", re.compile(r"^läuft (?:ein|irgendein|gerade ein|noch ein) timer$")),
        ("timer_list", re.compile(r"^laufen (?:gerade |noch )?timer$")),
        ("timer_list", re.compile(r"^wie viele timer (?:habe ich|laufen|sind gestellt)(?: gerade)?$")),
        ("timer_list", re.compile(r"^(?:zeig|zeige)(?: mir)? (?:meine|die|alle) timer$")),
        ("timer_cancel", re.compile(rf"^(?:stopp|stoppe|stop|lösch|lösche|brich|brech|beende|entferne)(?: mir| mal| bitte| doch)* {_DE_T}(?: ab)?$")),
        ("timer_cancel", re.compile(rf"^(?:schalte|stell|stelle)(?: mir| mal| bitte| doch)* {_DE_T} (?:ab|aus)$")),
        ("timer_cancel", re.compile(rf"^{_DE_T} (?:abbrechen|löschen|stoppen|beenden|entfernen|ausschalten|abstellen)$")),
        ("timer_cancel", re.compile(rf"^{_DE_T} (?:für|namens) (?P<q2>[a-zäöüß0-9 ]{{1,40}}) (?:abbrechen|löschen|stoppen|beenden|entfernen)$")),
        ("timer_cancel", re.compile(rf"^(?:lösch|lösche|stopp|stoppe|brich|beende)(?: mir| mal| bitte)* {_DE_T} (?:für|namens) (?P<q2>[a-zäöüß0-9 ]{{1,40}})(?: ab)?$")),
    ],
}
_PLURAL_ART = {"all my", "all the", "all", "every", "alle", "alle meine", "die", "meine", "jeden"}

_STOP_Q = {"my", "the", "that", "this", "a", "an", "mein", "meinen", "meinem", "meine", "meiner", "dein", "der", "die", "das", "den", "dem",
           "diesen", "diesem", "diese", "einen", "ein", "eine"}


def _query(kind: str, m: re.Match, lang: str) -> tuple[str, dict]:
    if kind != "timer_cancel" and kind != "timer_left":
        return kind, {}
    q = (m.groupdict().get("q2") or m.groupdict().get("q") or "").strip()
    art, noun = m.groupdict().get("art") or "", m.group("noun")
    every = art in _PLURAL_ART or (noun == "timers") or q in ("all", "every", "alle", "alles") or q.startswith(("all ", "alle "))
    if q in ("all", "every", "alle", "alles") or q.startswith(("all ", "alle ")):
        q = ""
    q = " ".join(w for w in q.split() if w not in _STOP_Q)
    if every and not q:
        return kind, {"all": True}
    if not q:
        return kind, {}
    secs, used = parse_duration(q.split(), lang)
    if secs is not None and used == len(q.split()):
        return kind, {"seconds": secs}
    return kind, {"q": q}


def parse(core: str, original: str, languages: tuple[str, ...]) -> tuple[str, str, dict] | None:
    """(kind, language, data) for a timer sentence, else None. core: the sentence lowercased without punctuation (fastpath._clean);
    original: the owner's own text (for the capital letters of a label)."""
    if "timer" not in core and "erinner" not in core and "remind" not in core and "reminder" not in core and "alarm" not in core \
            and "erinnern" not in core:
        return None
    if len(core.split()) > MAX_WORDS:
        return None
    for lang in languages:
        t = _P[lang].sub("", core, count=1)
        for kind, rx in _ASK[lang]:
            m = rx.fullmatch(t)
            if m:
                k, data = _query(kind, m, lang)
                return k, lang, data
        for kind, rx in _SET[lang]:
            m = rx.fullmatch(t)
            if not m:
                continue
            groups = m.groupdict()
            tokens = (groups["rest"] or "").split()
            secs, used = parse_duration(tokens, lang)
            if secs is None:
                continue
            tail = (groups.get("tail") or "").split()
            if "tail" in groups:                                   # "set a 20 minute timer [for the tea]": the length is all of `rest`
                if used != len(tokens):
                    continue
                tokens, used = tail, 0
            ok, label = _label(tokens, used, lang, kind, original)
            if not ok:
                continue
            return "timer_set", lang, {"seconds": secs, "label": label, "reminder": kind == "reminder"}
    return None
