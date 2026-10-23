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

import re

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
_BEFORE = r"(?<![\w.,:/\\@#$€£'’+-])(?<!\d[   ])"
_AFTER = r"(?![\w@/\\]|[.,:]\d|-\w|[   ]\d)"
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


# ---------------------------------------------------------------- German

_FEMININE = re.compile(r"(?:ung|keit|heit|schaft|tion|ität|ik|ur|ie)$|^(?:Stunde|Minute|Sekunde|Woche|Nachricht|Mail|"
                       r"E-Mail|Aufgabe|Notiz|Frage|Erinnerung|Seite|Zeile|Zahl|Sache|Person|Karte|Rechnung|Tasche|"
                       r"Datei|Liste|Nummer|Stelle|Runde|Gruppe)$")
_EIN = {"Uhr", "Tag", "Termin", "Monat", "Jahr", "Mal", "Moment", "Euro", "Franken", "Rappen", "Cent", "Dollar",
        "Kilometer", "Meter", "Liter", "Gramm", "Kilo", "Grad", "Stück", "Zimmer", "Platz", "Stock", "Ticket", "Treffen",
        "Spiel", "Thema", "Problem", "Projekt", "Event", "Foto", "Dokument", "Kind", "Buch", "Fach", "Test"}


def _one_de(rest: str) -> str | None:
    """How a lone 1 is said before the words in `rest`: "ein", "eine", or None when the gender is unclear (the
    digit stays; the engine says "eins", as it always did)."""
    for word in re.findall(r"[A-Za-zÄÖÜäöüß-]+", rest[:60])[:1]:
        if word[0].isupper() and _FEMININE.search(word):
            return "eine"
        if word in _EIN:
            return "ein"
        if word[0].islower():                          # an adjective ("neue", "wichtiger"): its ending has the gender
            return "eine" if word.endswith("e") else "ein" if word.endswith(("es", "er")) else None
    return None


def _german(t: str) -> str:
    t = _NEG.sub("minus ", t)
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
    if n == 1:
        one = _one_de(m.string[m.end():])
        return one or m.group(0)
    return de_cardinal(n)


# ---------------------------------------------------------------- English

def _english(t: str) -> str:
    t = _NEG.sub("minus ", t)
    t = _DEC_EN.sub(lambda m: _decimal(m, en_cardinal, "point", _EN_TO19), t)
    t = _SWISS_THOUSANDS.sub(lambda m: re.sub(r"['’]", "", m.group(0)), t)
    return _PERCENT.sub(" percent", t)


# ---------------------------------------------------------------- entry point

_NEEDS_WORK = re.compile(r"[\d%]")


def normalize(text: str, lang: str | None = None) -> str:
    """`text` as it should be said. lang: "en" or "de"; None finds it from the words (as the voice does). Text
    without a digit or a percent sign is returned as it is (the same object). Other languages are not touched."""
    if not text or not _NEEDS_WORK.search(text):
        return text
    if lang is None:
        from .voice import language_of
        lang = language_of(text)
    if lang == "de":
        return _german(text)
    return _english(text) if lang == "en" else text
