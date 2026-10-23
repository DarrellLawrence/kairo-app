"""Voice G9: speech-text normalisation (app/speaknorm.py). What Kairo says is not what it shows: digits, times,
dates and amounts are written out in words before the voices get them, because the engines' own front ends
(espeak-ng under Piper and Kokoro) read them wrongly (measured on both front ends, see the module's docstring).
Everything here is made up; nothing is a real person's date, amount or number."""

from __future__ import annotations

import re

import pytest

from app.speaknorm import de_cardinal, de_ordinal, en_cardinal, en_ordinal, normalize

# ---------------------------------------------------------------- number words


@pytest.mark.parametrize("n,words", [
    (0, "null"), (1, "eins"), (7, "sieben"), (12, "zwölf"), (14, "vierzehn"), (16, "sechzehn"), (17, "siebzehn"),
    (20, "zwanzig"), (21, "einundzwanzig"), (25, "fünfundzwanzig"), (30, "dreißig"), (31, "einunddreißig"),
    (80, "achtzig"), (99, "neunundneunzig"), (100, "einhundert"), (101, "einhunderteins"), (204, "zweihundertvier"),
    (999, "neunhundertneunundneunzig"), (1000, "eintausend"), (1001, "eintausendeins"),
    (1250, "eintausendzweihundertfünfzig"), (2026, "zweitausendsechsundzwanzig"),
    (21000, "einundzwanzigtausend"), (100000, "einhunderttausend"),
    (1000000, "eine Million"), (2500000, "zwei Millionen fünfhunderttausend"),
])
def test_german_cardinals(n, words):
    assert de_cardinal(n) == words


def test_a_german_one_before_a_noun_is_ein_or_eine():
    assert de_cardinal(1, "ein") == "ein" and de_cardinal(1, "eine") == "eine"
    assert de_cardinal(21, "eine") == "einundzwanzig"                   # only a final 1 changes


@pytest.mark.parametrize("n,ending,word", [
    (1, "en", "ersten"), (2, "en", "zweiten"), (3, "en", "dritten"), (3, "e", "dritte"), (7, "en", "siebten"),
    (8, "en", "achten"), (12, "en", "zwölften"), (13, "en", "dreizehnten"), (19, "en", "neunzehnten"),
    (20, "en", "zwanzigsten"), (21, "e", "einundzwanzigste"), (23, "en", "dreiundzwanzigsten"),
    (30, "en", "dreißigsten"), (31, "en", "einunddreißigsten"),
])
def test_german_ordinals(n, ending, word):
    assert de_ordinal(n, ending) == word


@pytest.mark.parametrize("n,words", [
    (0, "zero"), (7, "seven"), (14, "fourteen"), (21, "twenty-one"), (40, "forty"), (100, "one hundred"),
    (123, "one hundred twenty-three"), (1250, "one thousand two hundred fifty"), (12000, "twelve thousand"),
    (1000000, "one million"),
])
def test_english_cardinals(n, words):
    assert en_cardinal(n) == words


@pytest.mark.parametrize("n,word", [
    (1, "first"), (2, "second"), (3, "third"), (4, "fourth"), (5, "fifth"), (8, "eighth"), (9, "ninth"),
    (11, "eleventh"), (12, "twelfth"), (20, "twentieth"), (21, "twenty-first"), (22, "twenty-second"),
    (23, "twenty-third"), (30, "thirtieth"), (31, "thirty-first"),
])
def test_english_ordinals(n, word):
    assert en_ordinal(n) == word


# ---------------------------------------------------------------- the digit lines of the research (R/refute_tts_de/lines.py)
# (text with digits, the spelled form the research used to show that Piper reads words right)
LINES = [
    ("Dein Termin ist um 14:30 Uhr.", "Dein Termin ist um vierzehn Uhr dreißig."),
    ("Der Zug fährt um 8:15 Uhr ab.", "Der Zug fährt um acht Uhr fünfzehn ab."),
    ("Das Meeting beginnt um 9 Uhr.", "Das Meeting beginnt um neun Uhr."),
    ("Das kostet 12,50 Franken.", "Das kostet zwölf Franken fünfzig."),
    ("Es sind 3,5 Kilometer bis zum Bahnhof.", "Es sind drei Komma fünf Kilometer bis zum Bahnhof."),
    ("Am 3. November habe ich frei.", "Am dritten November habe ich frei."),
    ("Der 21. Oktober war ein Dienstag.", "Der einundzwanzigste Oktober war ein Dienstag."),
    ("Du hast 7 neue Nachrichten.", "Du hast sieben neue Nachrichten."),
    ("Es sind 25 Leute eingeladen.", "Es sind fünfundzwanzig Leute eingeladen."),
    ("Ich erinnere dich in 20 Minuten daran.", "Ich erinnere dich in zwanzig Minuten daran."),
    ("Der Akku hat noch 80 Prozent.", "Der Akku hat noch achtzig Prozent."),
    ("Im Jahr 2026 ist viel passiert.", "Im Jahr zweitausendsechsundzwanzig ist viel passiert."),
    ("Die Rechnung beträgt 1250 Franken.", "Die Rechnung beträgt eintausendzweihundertfünfzig Franken."),
    ("Der Test ist am 23.10.2026.", "Der Test ist am dreiundzwanzigsten Oktober zweitausendsechsundzwanzig."),
]
# stage 1 (integers, decimals): the lines that need no clock, calendar or money rule
_STAGE1 = {"Das Meeting beginnt um 9 Uhr.", "Es sind 3,5 Kilometer bis zum Bahnhof.", "Du hast 7 neue Nachrichten.",
           "Es sind 25 Leute eingeladen.", "Ich erinnere dich in 20 Minuten daran.", "Im Jahr 2026 ist viel passiert.",
           "Die Rechnung beträgt 1250 Franken."}


@pytest.mark.parametrize("digits,spelled", [x for x in LINES if x[0] in _STAGE1])
def test_the_research_lines_read_as_words(digits, spelled):
    assert normalize(digits, "de") == spelled


# ---------------------------------------------------------------- German integers, decimals, percent

def test_german_integers_become_one_word_each():
    assert normalize("Es sind 14 Grad und 21 Wolken.", "de") == "Es sind vierzehn Grad und einundzwanzig Wolken."
    assert normalize("Das sind 100 Franken, nicht 101.", "de") == "Das sind einhundert Franken, nicht einhunderteins."
    assert normalize("Es sind 3 Millionen.", "de") == "Es sind drei Millionen."
    assert normalize("Seite 204 von 1.250.", "de") == "Seite zweihundertvier von eintausendzweihundertfünfzig."
    assert normalize("Das sind 1'250 Franken.", "de") == "Das sind eintausendzweihundertfünfzig Franken."   # Swiss thousands
    assert normalize("Es sind 0 Treffer.", "de") == "Es sind null Treffer."


def test_a_german_one_is_ein_eine_or_left_alone():
    assert normalize("Du hast 1 neue Nachricht.", "de") == "Du hast eine neue Nachricht."
    assert normalize("Ich brauche 1 Stunde.", "de") == "Ich brauche eine Stunde."
    assert normalize("Es ist 1 Uhr.", "de") == "Es ist ein Uhr."
    assert normalize("Du hast 1 wichtigen Termin und 1 Termin.", "de") == "Du hast 1 wichtigen Termin und ein Termin."
    assert normalize("Platz 1.", "de") == "Platz 1."                      # unknown: the engine says "eins", as before
    assert normalize("Du hast 1 Ding.", "de") == "Du hast 1 Ding."


def test_german_decimals_say_komma_and_the_digits_one_by_one():
    assert normalize("Es sind 3,5 Kilometer.", "de") == "Es sind drei Komma fünf Kilometer."
    assert normalize("Das sind 0,75 Liter.", "de") == "Das sind null Komma sieben fünf Liter."
    assert normalize("Das sind 1,05 Meter.", "de") == "Das sind eins Komma null fünf Meter."
    assert normalize("Das sind 1.250,5 Meter.", "de") == "Das sind eintausendzweihundertfünfzig Komma fünf Meter."
    assert normalize("Es sind -3,5 Grad.", "de") == "Es sind minus drei Komma fünf Grad."


def test_german_percent_is_said_as_the_word():
    assert normalize("Der Akku hat noch 80% und 12,5 %.", "de") == "Der Akku hat noch achtzig Prozent und zwölf Komma fünf Prozent."


# ---------------------------------------------------------------- English decimals, percent

def test_english_decimals_say_point_and_the_digits_one_by_one():
    assert normalize("It is 3.5 kilometers.", "en") == "It is three point five kilometers."
    assert normalize("It is 0.75 liters.", "en") == "It is zero point seven five liters."
    assert normalize("It is 1,250.5 meters.", "en") == "It is one thousand two hundred fifty point five meters."
    assert normalize("It is -3.5 degrees.", "en") == "It is minus three point five degrees."
    assert normalize("The battery is at 80%.", "en") == "The battery is at 80 percent."
    assert normalize("That is 1'250 francs.", "en") == "That is 1250 francs."


def test_english_integers_are_left_to_the_engine():
    s = "You have 7 new messages, 20 minutes left, 1,250 francs, in 2026, on the 23rd."
    assert normalize(s, "en") == s


# ---------------------------------------------------------------- what must not change

@pytest.mark.parametrize("lang", ["en", "de"])
@pytest.mark.parametrize("text", [
    "", "Sure, I'll remind you tomorrow morning.", "Klar, ich erinnere dich morgen früh.", "Okay.", "Dr. Weber is on leave.",
])
def test_text_without_digits_is_returned_unchanged(text, lang):
    assert normalize(text, lang) is text


@pytest.mark.parametrize("text", [
    "Room B2 on floor A4.", "Der mp3-Player und das 3D-Modell.", "COVID-19 und H2O.", "Ticket AB-12345 ist offen.",
    "Mail an kairo2@example.com bitte.", "Siehe https://example.com/2026/10/23/item-7 dazu.", "Der Server 192.168.1.20 antwortet.",
    "Version 1.2.3 ist da.", "Datei report_2026.pdf liegt bereit.", "Ruf 044 123 45 67 an.", "Die 20er Jahre, 5-jährig, 3x so viel.",
    "Siehe #12 und L23.", "Zahlen 1,2,3 gehen.", "Der Hash a3f9c2 ist neu.", "Um 25:30 Uhr passiert nichts.", "Es sind 1234567890123 Sterne.",
])
def test_numbers_inside_names_ids_and_codes_are_left_alone(text):
    for lang in ("en", "de"):
        out = normalize(text, lang)
        # no digit of those tokens is turned into a word: every digit run is still there
        assert re.findall(r"\d+", out) == re.findall(r"\d+", text), (lang, out)


def test_the_language_is_found_from_the_text_when_it_is_not_given():
    assert normalize("Du hast 7 neue Nachrichten und ich bin da.") == "Du hast sieben neue Nachrichten und ich bin da."
    assert normalize("You have 3.5 hours and I am here.") == "You have three point five hours and I am here."
    assert normalize("Some 7 words", "fr") == "Some 7 words"                  # only the two voices' languages are touched


def test_no_digit_or_percent_sign_is_left_that_the_rules_know():
    assert not re.search(r"[\d%]", normalize("Es sind 3,5 km und 80% von 1250 Franken.", "de"))
    assert normalize("100%", "de") == "einhundert Prozent"
