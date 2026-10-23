"""Speech-text normalisation (voice plan G9): numbers, times, dates and amounts written out in words before
Kairo's voices say them. What is shown on the phone stays as written; only what goes to the voice changes.

Why: both voices read digits through espeak-ng, whose rules get everyday things wrong. Measured on the engines'
own front ends (the phonemes they make; sandbox, 2026-10-23):
  German (Piper):  "14:30 Uhr" -> "vierzehn Uhr dreißig Uhr" (Uhr twice); "3. November" -> "drei." and a new
                   sentence "November"; "12,50 Franken" -> "zwölf Komma fünf null Franken"; "23.10.2026" ->
                   "dreiundzwanzig Punkt zehn Punkt ..."; "1'250" -> "eins zweihundertfünfzig"; "1 Stunde" ->
                   "eins Stunde"; "21" and "2026" come out as separate words; "14" with a short i.
  English (Kokoro): "9:00" -> "nine zero zero"; "14:10" -> "fourteen, ten" (a colon pause); "$12.50" -> "dollar
                   twelve. fifty"; "3.5" -> "three. five"; "23.10.2026" -> "twenty-three. ten point two zero ...".
English integers, "23rd" and "1,250" are read right and are left alone.

In-house on purpose: num2words would be a new (LGPL) dependency. Pure functions, no I/O, nothing personal.
The rule for anything unsure is to leave it as it was: a number glued to letters or joined to other
characters (B2, mp3, COVID-19, AB-12345, 192.168.1.20, 1.2.3, /2026/10/23, 044 123 45 67, 1,2,3), more than
nine digits, a number with a leading zero, an hour past 24 or a minute past 59 is not touched.

Switched on by the owner's flag g9.speaknorm (default off until the voices were listened to on the PC); calls
read it once through for_call() and hand the result to the speaker (voicertc.KairoTTS, voicelink.Speaker).
"""

from __future__ import annotations

import logging
import re
from typing import Callable

log = logging.getLogger(__name__)

FLAG = "g9.speaknorm"
FLAG_EN_DIGIT_TIMES = "g9.speaknorm_en_digit_times"

# ---------------------------------------------------------------- number words

_DE_TO19 = ("null eins zwei drei vier fünf sechs sieben acht neun zehn elf zwölf dreizehn vierzehn fünfzehn "
            "sechzehn siebzehn achtzehn neunzehn").split()
_DE_TENS = "- - zwanzig dreißig vierzig fünfzig sechzig siebzig achtzig neunzig".split()
_DE_ORD = {1: "erst", 2: "zweit", 3: "dritt", 4: "viert", 5: "fünft", 6: "sechst", 7: "siebt", 8: "acht", 9: "neunt",
           10: "zehnt", 11: "elft", 12: "zwölft"}
_EN_TO19 = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
            "seventeen eighteen nineteen").split()
_EN_TENS = "- - twenty thirty forty fifty sixty seventy eighty ninety".split()
_EN_ORD = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth", "nine": "ninth",
           "twelve": "twelfth"}


def _de_100(n: int, one: str) -> str:
    if n < 20:
        return one if n == 1 else _DE_TO19[n]
    tens, ones = divmod(n, 10)
    return _DE_TENS[tens] if not ones else ("ein" if ones == 1 else _DE_TO19[ones]) + "und" + _DE_TENS[tens]


def de_cardinal(n: int, one: str = "eins") -> str:
    """0 .. 999,999,999 in German, as the research spelled it ("eintausendzweihundertfünfzig"). `one`: how a final
    1 is said ("eins", or "ein" / "eine" before a noun); a 1 inside a number is always "ein"."""
    if n < 100:
        return _de_100(n, one)
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return _de_100(hundreds, "ein") + "hundert" + (_de_100(rest, one) if rest else "")
    if n < 10**6:
        thousands, rest = divmod(n, 1000)
        return de_cardinal(thousands, "ein") + "tausend" + (de_cardinal(rest, one) if rest else "")
    millions, rest = divmod(n, 10**6)
    head = "eine Million" if millions == 1 else de_cardinal(millions, "ein") + " Millionen"
    return head + (" " + de_cardinal(rest, one) if rest else "")


def de_ordinal(n: int, ending: str = "en") -> str:
    """1 .. 99: "dritten" (ending "en": after am, zum, den, dem ...) or "dritte" (ending "e": after der, die, das)."""
    if n in _DE_ORD:
        stem = _DE_ORD[n]
    elif n < 20:
        stem = _DE_TO19[n] + "t"
    else:
        stem = de_cardinal(n) + "st"
    return stem + ending


def en_cardinal(n: int) -> str:
    """0 .. 999,999,999 in English ("one thousand two hundred fifty", "twenty-one")."""
    if n < 20:
        return _EN_TO19[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _EN_TENS[tens] + ("-" + _EN_TO19[ones] if ones else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return _EN_TO19[hundreds] + " hundred" + (" " + en_cardinal(rest) if rest else "")
    for unit, name in ((10**6, "million"), (1000, "thousand")):
        if n >= unit:
            head, rest = divmod(n, unit)
            return en_cardinal(head) + " " + name + (" " + en_cardinal(rest) if rest else "")
    raise ValueError(n)


def en_ordinal(n: int) -> str:
    """1 .. 99: "first", "twenty-third"."""
    front, last = re.match(r"(.*?)([a-z]+)$", en_cardinal(n)).groups()
    last = _EN_ORD.get(last) or (last[:-1] + "ieth" if last.endswith("y") else last + "th")
    return front + last


def _digits(s: str, words: list[str]) -> str:
    return " ".join(words[int(c)] for c in s)


# ---------------------------------------------------------------- shared rules

_NEG = re.compile(r"(?<![\w.,:/\\@#'’+-])-(?=\d)")                    # "-3,5": minus, not a hyphen
_PERCENT = re.compile(r"(?<=\w)\s?%(?!\w)")
# A number that stands alone: not glued to letters, not joined to other characters (IDs, versions, addresses,
# phone numbers), and not a part of a longer number written with spaces ("044 123 45 67").
_BEFORE_WORD = r"(?<![\w.,:/\\@#$€£'’+-])"
_BEFORE = _BEFORE_WORD + r"(?<!\d[ \u00a0\u202f])"
_AFTER = r"(?![\w@/\\]|[.,:]\d|-\w|[ \u00a0\u202f]\d)"
_INT_DE = re.compile(_BEFORE + r"(\d{1,3}(?:[.'’]\d{3})+|\d+)" + _AFTER)
_DEC_DE = re.compile(_BEFORE + r"(\d{1,3}(?:\.\d{3})+|\d+),(\d+)(?![\w@/\\]|[.,:]\d|-\w)")
_DEC_EN = re.compile(_BEFORE + r"(\d{1,3}(?:,\d{3})+|\d+)\.(\d+)(?![\w@/\\]|\.\d|-\w)")
_SWISS_THOUSANDS = re.compile(r"(?<![\w.,])\d{1,3}(?:['’]\d{3})+(?![\w])")


def _whole(token: str) -> int | None:
    """The integer in "1'250", "1.250", "1,250", "12": None when it is longer than nine digits or has a leading zero."""
    digits = re.sub(r"[.,'’]", "", token)
    if len(digits) > 9 or (len(digits) > 1 and digits[0] == "0"):
        return None
    return int(digits)


_DE_MONTHS = "Januar Februar März April Mai Juni Juli August September Oktober November Dezember".split()
_EN_MONTHS = "January February March April May June July August September October November December".split()
_DE_MONTH_OF = {"jan": 0, "feb": 1, "mär": 2, "mrz": 2, "apr": 3, "mai": 4, "jun": 5, "jul": 6, "aug": 7, "sep": 8,
                "okt": 9, "nov": 10, "dez": 11}                       # by the first three letters: "Okt." -> October
_MONTH_DE = (r"(?:Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember|"
             r"(?:Jan|Feb|Mär|Mrz|Apr|Jun|Jul|Aug|Sept?|Okt|Nov|Dez)\.?)(?![a-zäöüß])")
_VERSIONISH = {"version", "v", "ver", "python", "ios", "android", "windows", "build", "release", "node", "java", "port"}
# "3.11." without a year is a date only after one of these ("Raum 2.01." is a room); with a year it always is.
_DATE_WORDS = {"am", "bis", "ab", "vom", "von", "zum", "zu", "seit", "vor", "nach", "den", "dem", "der", "beim", "bei", "und",
               "oder", "für", "zwischen", "ist", "war", "wäre", "auf", "gegen", "um", "ende", "anfang", "mitte", "datum", "termin",
               "frist", "stichtag", "deadline", "geburtstag", "fällig", "montag", "dienstag", "mittwoch", "donnerstag", "freitag",
               "samstag", "sonntag", "mo", "di", "mi", "do", "fr", "sa", "so"}
_ISO = re.compile(r"(?<![\w.:/-])(\d{4})-(\d{2})-(\d{2})(?![\w-]|[.:/]\d)")


def _valid_date(day: int, month: int) -> bool:
    return 1 <= day <= 31 and 1 <= month <= 12


_CODE = {"CHF": "CHF", "SFr.": "CHF", "Fr.": "CHF", "EUR": "EUR", "€": "EUR", "USD": "USD", "$": "USD", "GBP": "GBP", "£": "GBP",
         "Franken": "CHF", "Euro": "EUR", "Dollar": "USD", "Pfund": "GBP"}
_UNITS = {"de": {"CHF": ("Franken", "Rappen"), "EUR": ("Euro", "Cent"), "USD": ("Dollar", "Cent"), "GBP": ("Pfund", "Pence")},
          "en": {"CHF": ("franc", "francs", "rappen", "rappen"), "EUR": ("euro", "euros", "cent", "cents"),
                 "USD": ("dollar", "dollars", "cent", "cents"), "GBP": ("pound", "pounds", "penny", "pence")}}
_CUR = r"CHF|EUR|USD|GBP|SFr\.|Fr\.|€|\$|£"
_AMT = r"\d[\d'’.,]*\d|\d"


def _money_re(words: str) -> re.Pattern:
    return re.compile(rf"{_BEFORE_WORD}(?:(?P<c1>{_CUR})\s?(?P<a1>{_AMT})|(?P<a2>{_AMT})\s?(?P<c2>{_CUR}{words}))(?![\w]|[.,]\d)")


_MONEY = {"de": _money_re("|Franken|Euro|Dollar|Pfund"), "en": _money_re("")}
_AMOUNT = {"de": re.compile(r"(\d{1,3}(?:\.\d{3})+|\d+)(?:[,.](\d{1,2}))?"), "en": re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?")}


def _amount(raw: str, lang: str) -> tuple[int, int | None] | None:
    """("1'250.50" | "1.250,50" | "12,5") -> (1250, 50) / (12, 50): whole units and cents (None: no cents written).
    German: the comma is the decimal sign (Swiss writing "12.50" works too), English: the dot."""
    m = _AMOUNT[lang].fullmatch(re.sub(r"['’]", "", raw))
    whole = _whole(m[1]) if m else None
    return None if whole is None else (whole, None if m[2] is None else int(m[2].ljust(2, "0")))


def _money(m: re.Match, lang: str) -> str:
    """"12,50 CHF" -> "zwölf Franken fünfzig", "$12.50" -> "twelve dollars fifty". A written-out currency word
    ("12 Franken") is only taken with cents: the plain integer rules say that already."""
    word = m["c2"] in ("Franken", "Euro", "Dollar", "Pfund")
    parsed = _amount(m["a1"] or m["a2"], lang)
    if parsed is None or (word and parsed[1] is None):
        return m.group(0)
    whole, cents = parsed
    unit = _UNITS[lang][_CODE[m["c1"] or m["c2"]]]
    end = "." if m.group(0).endswith(".") and m.end() == len(m.string.rstrip()) else ""     # "... 12.50 Fr." ends the sentence
    if lang == "de":
        if whole == 0 and cents:
            return f"{de_cardinal(cents, 'ein')} {unit[1]}{end}"
        return f"{de_cardinal(whole, 'ein')} {unit[0]}" + (f" {de_cardinal(cents)}" if cents else "") + end
    if whole == 0 and cents:
        return f"{en_cardinal(cents)} {unit[2] if cents == 1 else unit[3]}{end}"
    return f"{en_cardinal(whole)} {unit[0] if whole == 1 else unit[1]}" + (f" {en_cardinal(cents)}" if cents else "") + end


# ---------------------------------------------------------------- German

_FEMININE = re.compile(r"(?:ung|keit|heit|schaft|tion|ität|ik|ur|ie)$|^(?:Stunde|Minute|Sekunde|Woche|Nachricht|Mail|"
                       r"E-Mail|Aufgabe|Notiz|Frage|Erinnerung|Seite|Zeile|Zahl|Sache|Person|Karte|Rechnung|Tasche|"
                       r"Datei|Liste|Nummer|Stelle|Runde|Gruppe)$")
_NEUTER = {"Mal", "Jahr", "Zimmer", "Spiel", "Treffen", "Ticket", "Thema", "Problem", "Projekt", "Event", "Foto", "Dokument",
           "Kind", "Buch", "Fach", "Stück", "Gramm", "Kilo", "Semester", "Quartal", "Kapitel"}
_MASCULINE = {"Tag", "Termin", "Monat", "Moment", "Euro", "Franken", "Rappen", "Cent", "Dollar", "Kilometer", "Meter", "Liter",
              "Grad", "Platz", "Stock", "Test", "Versuch", "Anruf", "Brief", "Besuch", "Kurs", "Auftrag"}
_DATIVE_AFTER = {"in", "vor", "nach", "seit", "mit", "bei", "von", "aus", "zu", "ab", "an", "binnen", "innerhalb", "außer"}


def _prev_word(text: str, pos: int) -> str:
    found = re.findall(r"[\wäöüÄÖÜß]+", text[max(0, pos - 30):pos])
    return found[-1].lower() if found else ""


def _one_de(m: re.Match) -> str | None:
    """A lone 1 before a noun, as it is said there: "eine Stunde", "ein Mal", "einen Termin", "in einer Minute".
    None when the gender is unclear (the digit stays and the engine says "eins", as it always did)."""
    words = re.findall(r"[A-Za-zÄÖÜäöüß-]+", m.string[m.end():m.end() + 60])[:1]
    if not words:
        return None
    word, dative = words[0], _prev_word(m.string, m.start()) in _DATIVE_AFTER
    if word == "Uhr":
        return "ein"
    if word[0].islower():                              # an adjective ("neue", "wichtigen"): its ending says the gender
        gender = {"e": "f", "es": "n", "er": "m", "en": "m"}.get(next((e for e in ("es", "er", "en", "e") if word.endswith(e)), ""))
    elif _FEMININE.search(word):
        gender = "f"
    else:
        gender = "n" if word in _NEUTER else "m" if word in _MASCULINE else None
    if gender is None:
        return None
    if dative:
        return "einer" if gender == "f" else "einem"
    return {"f": "eine", "n": "ein", "m": "ein" if word.endswith("er") and word[0].islower() else "einen"}[gender]


_ORD_NOUNS = {"Stock": "m", "Platz": "m", "Tag": "m", "Versuch": "m", "Rang": "m", "Monat": "m", "Mal": "n",
              "Semester": "n", "Quartal": "n", "Kapitel": "n", "Jahr": "n", "Jahrhundert": "n", "Woche": "f", "Klasse": "f",
              "Runde": "f", "Etage": "f", "Liga": "f", "Reihe": "f", "Zeile": "f", "Aufgabe": "f", "Stunde": "f",
              "Auflage": "f", "Lektion": "f", "Spalte": "f", "Seite": "f", "Weltkrieg": "m", "Advent": "m",
              "Geburtstag": "m", "Jahrestag": "m", "Spieltag": "m"}
_NUM_DATE_DE = re.compile(_BEFORE_WORD + r"(\d{1,2})\.(\d{1,2})\.(?:(\d{4})(?!\d|\.\d))?(?!\d)")
_DAY_MONTH_DE = re.compile(_BEFORE + r"(\d{1,2})\.\s*(" + _MONTH_DE + ")")
_DAY_RANGE_DE = re.compile(_BEFORE + r"(\d{1,2})\.\s*(?:[–-]\s*|(bis|und|oder)\s+)(?=\d{1,2}\.\s*" + _MONTH_DE + ")")
_DAY_NOUN_DE = re.compile(_BEFORE + r"(\d{1,2})\.\s*(" + "|".join(_ORD_NOUNS) + r")\b")
_YEAR_DE = re.compile(r"(?<![\w.,:/-])((?:" + "|".join(_DE_MONTHS) + r"|Jahr|Jahre|Jahres|Baujahr|Jahrgang|geboren|seit|im|bis|ab)\s+)"
                      r"(1[1-9]\d\d)(?![\w]|[.,:]\d)", re.I)
_CLOCK_RANGE_DE = re.compile(_BEFORE_WORD + r"(\d{1,2}:[0-5]\d(?:\s*Uhr)?)\s*[–−-]\s*(?=\d{1,2}:[0-5]\d)")
_HOUR_RANGE_DE = re.compile(_BEFORE_WORD + r"(\d{1,2})\s*[–−-]\s*(\d{1,2})(?=\s*Uhr\b)")
_TIME_DE = re.compile(_BEFORE_WORD + r"(\d{1,2})(?::([0-5]\d)(?!\d)|\.([0-5]\d)(?=\s*Uhr\b))(?:\s*Uhr\b)?(?![\w:])")


def _ordinal_de(n: int, text: str, pos: int, gender: str = "m") -> str:
    """"dritten" (after am, zum, den, dem ...) or "dritte" (after the article of the noun: der Oktober, die Aufgabe)."""
    article = {"m": "der", "f": "die", "n": "das"}[gender]
    return de_ordinal(n, "e" if _prev_word(text, pos) == article else "en")


def _year_de(y: int) -> str:
    """1100 .. 1999 as years are said ("neunzehnhundertfünf"); every other year is a plain number."""
    if 1100 <= y <= 1999:
        hundreds, rest = divmod(y, 100)
        return de_cardinal(hundreds) + "hundert" + (de_cardinal(rest) if rest else "")
    return de_cardinal(y)


def _date_de(m: re.Match, day: int, month: int, year: int | None) -> str:
    before = _prev_word(m.string, m.start())
    if not _valid_date(day, month) or before in _VERSIONISH or (year is None and before not in _DATE_WORDS):
        return m.group(0)
    return f"{_ordinal_de(day, m.string, m.start())} {_DE_MONTHS[month - 1]}" + (f" {_year_de(year)}" if year else "")


def _day_month_de(m: re.Match) -> str:
    day = int(m[1])
    if not 1 <= day <= 31:
        return m.group(0)
    return f"{_ordinal_de(day, m.string, m.start())} {_DE_MONTHS[_DE_MONTH_OF[m[2][:3].lower()]]}"


def _day_range_de(m: re.Match) -> str:
    day = int(m[1])
    return m.group(0) if not 1 <= day <= 31 else f"{_ordinal_de(day, m.string, m.start())} {m[2] or 'bis'} "


def _day_noun_de(m: re.Match) -> str:
    day = int(m[1])
    return m.group(0) if not 1 <= day <= 99 else f"{_ordinal_de(day, m.string, m.start(), _ORD_NOUNS[m[2]])} {m[2]}"


def _year_after_de(m: re.Match) -> str:
    year = int(m[2])
    if m[1].strip().lower() in ("geboren", "seit", "im", "bis", "ab") and year < 1900:
        return m.group(0)
    return m[1] + _year_de(year)


def _time_de(m: re.Match) -> str:
    hour, minute = int(m[1]), int(m[2] or m[3])
    if hour > 24 or (hour == 24 and minute):
        return m.group(0)
    return de_cardinal(hour, "ein") + " Uhr" + (f" {de_cardinal(minute)}" if minute else "")


def _german(t: str) -> str:
    t = _NEG.sub("minus ", t)
    t = _ISO.sub(lambda m: _date_de(m, int(m[3]), int(m[2]), int(m[1])), t)
    t = _NUM_DATE_DE.sub(lambda m: _date_de(m, int(m[1]), int(m[2]), int(m[3]) if m[3] else None), t)
    t = _DAY_RANGE_DE.sub(_day_range_de, t)
    t = _DAY_MONTH_DE.sub(_day_month_de, t)
    t = _DAY_NOUN_DE.sub(_day_noun_de, t)
    t = _YEAR_DE.sub(_year_after_de, t)
    t = _CLOCK_RANGE_DE.sub(r"\1 bis ", t)
    t = _HOUR_RANGE_DE.sub(lambda m: f"{de_cardinal(int(m[1]), 'ein')} bis {de_cardinal(int(m[2]), 'ein')}", t)
    t = _TIME_DE.sub(_time_de, t)
    t = _MONEY["de"].sub(lambda m: _money(m, "de"), t)
    t = _DEC_DE.sub(lambda m: (_decimal(m, de_cardinal, "Komma", _DE_TO19)), t)
    t = _INT_DE.sub(lambda m: _integer_de(m), t)
    return _PERCENT.sub(" Prozent", t)


def _decimal(m: re.Match, cardinal, point: str, words: list[str]) -> str:
    whole = _whole(m.group(1))
    return m.group(0) if whole is None else f"{cardinal(whole)} {point} {_digits(m.group(2), words)}"


def _integer_de(m: re.Match) -> str:
    n = _whole(m.group(1))
    if n is None:
        return m.group(0)
    return (_one_de(m) or m.group(0)) if n == 1 else de_cardinal(n)


# ---------------------------------------------------------------- English

_NUM_DATE_EN = re.compile(_BEFORE_WORD + r"(\d{1,2})\.(\d{1,2})\.(\d{4})(?![\w]|\.\d)")
_CLOCK_RANGE_EN = re.compile(_BEFORE_WORD + r"(\d{1,2}:[0-5]\d(?:\s?[ap]\.?m\.?)?)\s*[–−-]\s*(?=\d{1,2}:[0-5]\d)", re.I)
_TIME_EN = re.compile(_BEFORE_WORD + r"(\d{1,2})(?::([0-5]\d)|\.([0-5]\d)(?=\s?[ap](?:\.m\.|m)(?!\w)))"
                      r"(?:\s?([ap])(?:\.m\.|m)(?!\w))?(?![\w:])(?:\s+(noon|midnight)\b)?", re.I)


def _year_en(y: int) -> str:
    """1100 .. 1999 as years are said ("nineteen ninety-nine", "nineteen oh five"); later years are left to the engine."""
    if not 1100 <= y <= 1999:
        return str(y)
    hundreds, rest = divmod(y, 100)
    return en_cardinal(hundreds) + (" hundred" if not rest else " oh " + _EN_TO19[rest] if rest < 10 else " " + en_cardinal(rest))


def _date_en(m: re.Match, day: int, month: int, year: int) -> str:
    if not _valid_date(day, month):
        return m.group(0)
    return f"{_EN_MONTHS[month - 1]} {en_ordinal(day)}, {_year_en(year)}"


def _en_time(hour: int, minute: int, ap: str, style: str) -> str:
    """A clock time as it is said: "ten past two" (style "past": on the five, quarter and half hours; other minutes
    as "seven forty-one") or "two ten" (style "digits"). Hours are said on the 12-hour clock; "PM" when the text had it."""
    h12 = hour % 12 or 12
    tail = {"a": " AM", "p": " PM"}.get(ap, "")                 # (Kokoro reads "A M" as the article "a" + "M")
    if minute == 0:
        if (ap and h12 == 12) or (not ap and hour in (0, 12)):
            return "noon" if (ap == "p" or (not ap and hour == 12)) else "midnight"
        return en_cardinal(h12) + (tail or " o'clock")
    if style == "past" and minute % 5 == 0:
        nxt = en_cardinal(h12 % 12 + 1)
        if minute == 15:
            return f"quarter past {en_cardinal(h12)}{tail}"
        if minute == 30:
            return f"half past {en_cardinal(h12)}{tail}"
        if minute == 45:
            return f"quarter to {nxt}{tail}"
        return (f"{en_cardinal(minute)} past {en_cardinal(h12)}" if minute < 30 else f"{en_cardinal(60 - minute)} to {nxt}") + tail
    return f"{en_cardinal(h12)} {'oh ' + _EN_TO19[minute] if minute < 10 else en_cardinal(minute)}{tail}"


def _time_en(m: re.Match, style: str) -> str:
    hour, minute, ap = int(m[1]), int(m[2] or m[3]), (m[4] or "").lower()
    if (ap and not 1 <= hour <= 12) or (not ap and hour > 23):
        return m.group(0)
    return m[5].lower() if m[5] and minute == 0 else _en_time(hour, minute, ap, style)     # "12:00 noon" is "noon"


def _english(t: str, en_times: str) -> str:
    t = _NEG.sub("minus ", t)
    t = _ISO.sub(lambda m: _date_en(m, int(m[3]), int(m[2]), int(m[1])), t)
    t = _NUM_DATE_EN.sub(lambda m: _date_en(m, int(m[1]), int(m[2]), int(m[3])), t)
    t = _CLOCK_RANGE_EN.sub(r"\1 to ", t)
    t = _TIME_EN.sub(lambda m: _time_en(m, en_times), t)
    t = _MONEY["en"].sub(lambda m: _money(m, "en"), t)
    t = _DEC_EN.sub(lambda m: _decimal(m, en_cardinal, "point", _EN_TO19), t)
    t = _SWISS_THOUSANDS.sub(lambda m: re.sub(r"['’]", "", m.group(0)), t)
    return _PERCENT.sub(" percent", t)


# ---------------------------------------------------------------- entry point

_NEEDS_WORK = re.compile(r"[\d%]")


def normalize(text: str, lang: str | None = None, *, en_times: str = "past") -> str:
    """`text` as it should be said. lang: "en" or "de"; None finds it from the words (as the voice does). Text
    without a digit or a percent sign is returned as it is (the same object). Other languages are not touched.
    en_times: English clock times as "ten past two" ("past") or "two ten" ("digits"); chosen by listening."""
    if not text or not _NEEDS_WORK.search(text):
        return text
    if lang is None:
        from .voice import language_of
        lang = language_of(text)
    if lang == "de":
        return _german(text)
    return _english(text, en_times) if lang == "en" else text


# ---------------------------------------------------------------- what a call uses

def hook(en_times: str = "past") -> Callable[[str, str | None], tuple[str, str | None]]:
    """(text, language) -> (text to say, language to say it in): what a speaker hands to the voice. When the
    language was not known and the text changed, it is found from the original words here, because words alone
    ("vierzehn Uhr dreißig") may no longer show it. A bug in the rules never costs a sentence: the text is then
    said as written."""
    def say(text: str, lang: str | None = None) -> tuple[str, str | None]:
        try:
            out = normalize(text, lang, en_times=en_times)
            if out == text:
                return text, lang
            if lang is None:
                from .voice import language_of
                lang = language_of(text)
            return out, lang
        except Exception:
            log.exception("speaknorm: the text is said as written")        # (never the text: it may be personal)
            return text, lang
    return say


def for_call(s) -> Callable[[str, str | None], tuple[str, str | None]] | None:
    """The owner's switches (More -> System -> Experiments), read once when a call or the voice button starts:
    None while g9.speaknorm is off (the default)."""
    from . import flags
    if not flags.on(s, FLAG):
        return None
    return hook("digits" if flags.on(s, FLAG_EN_DIGIT_TIMES) else "past")
