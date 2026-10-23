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


@pytest.mark.parametrize("digits,spelled", LINES)
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


def test_a_german_one_takes_the_article_of_the_noun_after_it():
    assert normalize("Du hast 1 neue Nachricht.", "de") == "Du hast eine neue Nachricht."          # feminine
    assert normalize("Ich brauche 1 Stunde.", "de") == "Ich brauche eine Stunde."
    assert normalize("Es ist 1 Uhr.", "de") == "Es ist ein Uhr."                                   # the idiom
    assert normalize("Das war 1 Mal.", "de") == "Das war ein Mal."                                 # neuter
    assert normalize("Du hast 1 wichtigen Termin und 1 Termin.", "de") == "Du hast einen wichtigen Termin und einen Termin."
    assert normalize("Da ist 1 neuer Termin.", "de") == "Da ist ein neuer Termin."                 # the ending says: nominative
    assert normalize("Das kostet 1 Franken.", "de") == "Das kostet einen Franken."


def test_a_german_one_after_a_dative_preposition_is_einer_or_einem():
    assert normalize("In 1 Minute, vor 1 Woche, nach 1 Tag, seit 1 Jahr.", "de") == \
        "In einer Minute, vor einer Woche, nach einem Tag, seit einem Jahr."


def test_a_german_one_that_is_not_clear_stays_a_digit():
    assert normalize("Platz 1.", "de") == "Platz 1."                      # the engine says "eins", as before
    assert normalize("Du hast 1 Ding.", "de") == "Du hast 1 Ding."
    assert normalize("Es ist 1.", "de") == "Es ist 1."


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


# ---------------------------------------------------------------- German times

@pytest.mark.parametrize("text,said", [
    ("Dein Termin ist um 14:30 Uhr.", "Dein Termin ist um vierzehn Uhr dreißig."),         # not "... Uhr dreißig Uhr"
    ("Das Meeting beginnt um 9:00.", "Das Meeting beginnt um neun Uhr."),
    ("Das Meeting beginnt um 9:00 Uhr.", "Das Meeting beginnt um neun Uhr."),
    ("Um 0:05 Uhr ist Schluss.", "Um null Uhr fünf ist Schluss."),
    ("Um 1:30 und um 13:01.", "Um ein Uhr dreißig und um dreizehn Uhr eins."),
    ("Um 7:05 geht es los.", "Um sieben Uhr fünf geht es los."),
    ("Um 14.30 Uhr kommt der Bus.", "Um vierzehn Uhr dreißig kommt der Bus."),             # written the German way
    ("Von 9:00 bis 10:30 Uhr.", "Von neun Uhr bis zehn Uhr dreißig."),
    ("Von 9:00-10:30 Uhr.", "Von neun Uhr bis zehn Uhr dreißig."),
    ("Von 9-10 Uhr.", "Von neun bis zehn Uhr."),
    ("Bis 24:00 Uhr.", "Bis vierundzwanzig Uhr."),
])
def test_german_times(text, said):
    assert normalize(text, "de") == said


def test_a_time_that_is_none_is_left_alone():
    for text in ("Um 25:30 Uhr.", "Um 24:30 Uhr.", "Um 14:75 Uhr.", "Es stand 3:1.", "Um 14:30h.", "Um 12:30:15 Uhr."):
        assert normalize(text, "de") == text


# ---------------------------------------------------------------- German dates and ordinals

@pytest.mark.parametrize("text,said", [
    ("Am 3. November habe ich frei.", "Am dritten November habe ich frei."),
    ("Heute ist der 23. Oktober.", "Heute ist der dreiundzwanzigste Oktober."),            # "der" + a month: nominative
    ("Der 21. Oktober war ein Dienstag.", "Der einundzwanzigste Oktober war ein Dienstag."),
    ("Ab dem 1. Januar 2027.", "Ab dem ersten Januar zweitausendsiebenundzwanzig."),
    ("Am 31. Dezember ist Schluss.", "Am einunddreißigsten Dezember ist Schluss."),
    ("Am 23. Okt. 2026 ist es so weit.", "Am dreiundzwanzigsten Oktober zweitausendsechsundzwanzig ist es so weit."),
    ("Am 1. Mai 1999 war es so.", "Am ersten Mai neunzehnhundertneunundneunzig war es so."),
    ("Am 5. Mai 1905 war es so.", "Am fünften Mai neunzehnhundertfünf war es so."),
    ("Im Jahr 1999 war es so.", "Im Jahr neunzehnhundertneunundneunzig war es so."),
    ("Vom 3. bis 5. Mai ist frei.", "Vom dritten bis fünften Mai ist frei."),
    ("Vom 3.-5. Mai ist frei.", "Vom dritten bis fünften Mai ist frei."),
    ("Am 3. und 4. Juni ist frei.", "Am dritten und vierten Juni ist frei."),
])
def test_german_ordinal_dates(text, said):
    assert normalize(text, "de") == said


@pytest.mark.parametrize("text,said", [
    ("Der Test ist am 23.10.2026.", "Der Test ist am dreiundzwanzigsten Oktober zweitausendsechsundzwanzig."),
    ("Am 3.11. ist frei.", "Am dritten November ist frei."),
    ("Der Test ist am 2026-10-23.", "Der Test ist am dreiundzwanzigsten Oktober zweitausendsechsundzwanzig."),
    ("Der 03.04.1999 war ein Samstag.", "Der dritte April neunzehnhundertneunundneunzig war ein Samstag."),
    ("Vom 1.5. bis 3.5. ist zu.", "Vom ersten Mai bis dritten Mai ist zu."),
    ("Am Mo., 23.10. um 8:00.", "Am Mo., dreiundzwanzigsten Oktober um acht Uhr."),
    ("Frist: 15.3. ist fix.", "Frist: fünfzehnten März ist fix."),
])
def test_german_numeric_dates(text, said):
    assert normalize(text, "de") == said


def test_a_dotted_number_without_a_year_is_a_date_only_after_a_date_word():
    """"Raum 2.01." could be a date or a room; with a year, or after am / bis / vom / a weekday, it is a date."""
    for text in ("Zimmer 3.14 und Raum 2.01.", "Kapitel 4.10. Danach.", "Die Zahl 7.12. ist es."):
        assert normalize(text, "de") == text, text


@pytest.mark.parametrize("text,said", [
    ("Du bist im 2. Stock.", "Du bist im zweiten Stock."),
    ("Das war zum 3. Mal.", "Das war zum dritten Mal."),
    ("Der 2. Platz ist frei, den 1. Platz nimmt er.", "Der zweite Platz ist frei, den ersten Platz nimmt er."),
    ("Die 3. Aufgabe fehlt, bei der 3. Aufgabe hakt es.", "Die dritte Aufgabe fehlt, bei der dritten Aufgabe hakt es."),
    ("Der 2. Weltkrieg und am 1. Advent.", "Der zweite Weltkrieg und am ersten Advent."),
    ("Alles Gute zum 30. Geburtstag.", "Alles Gute zum dreißigsten Geburtstag."),
])
def test_german_ordinals_before_the_nouns_that_take_them(text, said):
    assert normalize(text, "de") == said


def test_a_number_with_a_dot_that_is_no_date_stays_a_sentence_end():
    assert normalize("Ich habe 3. Dann gehe ich.", "de") == "Ich habe drei. Dann gehe ich."
    assert normalize("Es gibt 32. Oktober nicht.", "de") == "Es gibt zweiunddreißig. Oktober nicht."   # no day 32
    assert normalize("Am 31.13. nicht.", "de") == "Am 31.13. nicht."                                  # no month 13
    assert normalize("Python 3.11. Danach.", "de") == "Python 3.11. Danach."                        # a version


# ---------------------------------------------------------------- English times

@pytest.mark.parametrize("text,said", [
    ("Your meeting is at 14:10.", "Your meeting is at ten past two."),
    ("It starts at 9:00.", "It starts at nine o'clock."),
    ("Your train leaves at 07:41.", "Your train leaves at seven forty-one."),
    ("Meet at 6:07.", "Meet at six oh seven."),
    ("Meet at 6:15 or 6:30.", "Meet at quarter past six or half past six."),
    ("Meet at 6:35, 6:45 or 6:55.", "Meet at twenty-five to seven, quarter to seven or five to seven."),
    ("It's 12:30 pm.", "It's half past twelve P M."),
    ("It starts at 9:00 am.", "It starts at nine A M."),
    ("It starts at 2:45 p.m. sharp.", "It starts at quarter to three P M sharp."),
    ("Noon is 12:00, midnight is 00:00 or 12:00 am.", "Noon is noon, midnight is midnight or midnight."),
    ("Meet at 12:45 or 0:05.", "Meet at quarter to one or five past twelve."),
    ("From 9:00-10:30.", "From nine o'clock to half past ten."),
    ("Call at 5.30 pm or 5:30pm.", "Call at half past five P M or half past five P M."),   # not "five point three zero"
    ("At 12:00 noon or 12:00 midnight.", "At noon or midnight."),
])
def test_english_times_are_said_as_a_clock(text, said):
    assert normalize(text, "en") == said


@pytest.mark.parametrize("text,said", [
    ("Your meeting is at 14:10.", "Your meeting is at two ten."),
    ("Meet at 9:05, 9:00 or 14:30.", "Meet at nine oh five, nine o'clock or two thirty."),
    ("It's 2:45 pm.", "It's two forty-five P M."),
])
def test_english_times_can_be_said_as_digits_too(text, said):
    assert normalize(text, "en", en_times="digits") == said


def test_an_english_time_that_is_none_is_left_alone():
    for text in ("At 25:30 now.", "At 13:00 pm now.", "At 14:75 now.", "At 14:30h now."):
        assert normalize(text, "en") == text


@pytest.mark.parametrize("text,said", [
    ("The test is on 23.10.2026.", "The test is on October twenty-third, 2026."),
    ("The test is on 2026-10-23.", "The test is on October twenty-third, 2026."),
    ("It was on 01.05.1999.", "It was on May first, 1999."),
])
def test_english_dates(text, said):
    assert normalize(text, "en") == said


# ---------------------------------------------------------------- amounts

@pytest.mark.parametrize("text,said", [
    ("Das kostet 12,50 Franken.", "Das kostet zwölf Franken fünfzig."),
    ("Das sind 12,50 CHF, Fr. 12.50, CHF 12.50 und 12.50 Fr.", "Das sind zwölf Franken fünfzig, zwölf Franken fünfzig, zwölf Franken fünfzig und zwölf Franken fünfzig."),
    ("Die Rechnung: 1'250.50 CHF.", "Die Rechnung: eintausendzweihundertfünfzig Franken fünfzig."),
    ("Dazu 1.250,50 € und 12,05 Euro.", "Dazu eintausendzweihundertfünfzig Euro fünfzig und zwölf Euro fünf."),
    ("Nur 0,50 Franken, 12,00 CHF, € 3,20.", "Nur fünfzig Rappen, zwölf Franken, drei Euro zwanzig."),
    ("$5.99 oder 5 $ oder £3.50 oder 1,5 CHF.", "fünf Dollar neunundneunzig oder fünf Dollar oder drei Pfund fünfzig oder ein Franken fünfzig."),
    ("Das sind 12 Franken, 100 Euro und 1 Euro.", "Das sind zwölf Franken, einhundert Euro und einen Euro."),   # no decimals: the integer rules
    ("Das sind 12 CHF und 1 CHF und CHF 1.", "Das sind zwölf Franken und ein Franken und ein Franken."),
])
def test_german_amounts(text, said):
    assert normalize(text, "de") == said


@pytest.mark.parametrize("text,said", [
    ("It costs $12.50.", "It costs twelve dollars fifty."),
    ("It costs CHF 12.50 or 12.50 CHF.", "It costs twelve francs fifty or twelve francs fifty."),
    ("It costs €12.50 or £3.20.", "It costs twelve euros fifty or three pounds twenty."),
    ("Only $1 or $0.99 or $0.01.", "Only one dollar or ninety-nine cents or one cent."),
    ("Total $1,250.00 or $1,250.5.", "Total one thousand two hundred fifty dollars or one thousand two hundred fifty dollars fifty."),
    ("That is 5 USD and 7 EUR.", "That is five dollars and seven euros."),
])
def test_english_amounts(text, said):
    assert normalize(text, "en") == said


def test_amounts_that_are_unclear_are_left_alone():
    assert normalize("It costs 12,50 francs.", "en") == "It costs 12,50 francs."            # the engine reads this right
    assert normalize("Das sind 12,345 Franken.", "de") == "Das sind zwölf Komma drei vier fünf Franken."   # a decimal, not money
    assert normalize("Code CHF 1.2.3 gilt.", "de") == "Code CHF 1.2.3 gilt."
    assert normalize("Das sind 05,50 CHF.", "de") == "Das sind 05,50 CHF."
    assert normalize("Das sind 1234567890,50 CHF.", "de") == "Das sind 1234567890,50 CHF."


# ---------------------------------------------------------------- it never breaks a sentence

def test_whatever_is_thrown_at_it_comes_back_as_text_and_twice_through_is_still_text():
    """A voice must never fail on a piece of text: random digits, dots, colons, commas, signs and words."""
    import random
    rng = random.Random(20261023)
    atoms = ["0", "1", "7", "12", "24", "59", "60", "99", "2026", "1999", "123456789", "1234567890123", ":", ".", ",", "-", "–", "'", "/", " ", " ",
             " ", "Uhr", "am", "pm", "a.m.", "CHF", "Fr.", "€", "$", "%", "Mai", "Okt.", "Franken", "noon", "bis", "und", "Stock", "der", "Hallo"]
    for _ in range(4000):
        text = "".join(rng.choice(atoms) for _ in range(rng.randint(1, 14)))
        for lang in ("de", "en", None):
            once = normalize(text, lang)
            assert isinstance(once, str)
            assert isinstance(normalize(once, lang), str)
    for lang in ("de", "en"):
        assert isinstance(normalize("1" * 3000, lang), str) and isinstance(normalize("1:" * 3000, lang), str)
