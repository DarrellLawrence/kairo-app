"""G10 fast paths: questions a rule answers without a model (app/fastpath.py). A wrong rule answer is worse than a
slow one, so the tables below are mostly NEGATIVES: phrases that look like a fast-path question and are not, and must
go on to the voice model exactly as before. English and German, with and without politeness, the way Whisper writes it."""

from __future__ import annotations

import datetime as dt

import pytest

from app import fastpath
from app.fastpath import match, norm
from tests.test_pa import TZ

WED = dt.datetime(2026, 10, 14, 14, 35, tzinfo=TZ)           # Wednesday
SUN = dt.datetime(2026, 10, 25, 9, 5, tzinfo=TZ)


def kinds(phrases, lang_set=("en", "de")):
    return [(p, (lambda m: (m.kind, m.lang) if m else None)(match(p, lang_set))) for p in phrases]


# ---------------------------------------------------------------- what is recognised

TIME_EN = ["What time is it?", "what's the time", "Kairo, what time is it right now?", "Hey Kairo what's the time please",
           "what time is it now", "Can you tell me the time?", "Could you tell me what time it is?", "do you have the time",
           "What is the current time", "Do you know what time it is?", "okay what's the time", "WHAT TIME IS IT", "whats the time",
           "what time is it currently", "Tell me the time please", "got the time"]
TIME_DE = ["Wie spät ist es?", "Wie spät ist es jetzt?", "Kairo, wie viel Uhr ist es?", "wieviel Uhr ist es", "Sag mir bitte, wie spät es ist",
           "wie spät haben wir", "Okay Kairo, wie spät ist es?", "Kannst du mir sagen, wie spät es ist?", "wie viel Uhr haben wir",
           "Wie spät ist es gerade?", "Wie spät ist's?", "Welche Uhrzeit haben wir?", "Weißt du, wie spät es ist?", "Sag mir die Uhrzeit",
           "wie lautet die Uhrzeit", "Hallo Kairo, wie viel Uhr haben wir jetzt?"]
DATE_EN = ["What day is it?", "what day is it today", "What's the date?", "what's the date today", "what is the date today",
           "Kairo what date is it", "what's today's date", "Which day is it today?", "Can you tell me the date?", "what day is today"]
WEEKDAY_EN = ["what day of the week is it", "What day of the week is it today?"]
DATE_DE = ["Welcher Tag ist heute?", "Welches Datum haben wir heute?", "welches Datum ist heute", "Den wievielten haben wir?", "Hey Kairo, welcher Tag ist heute?",
           "welchen Tag haben wir heute", "Der Wievielte ist heute?", "Sag mir das Datum", "Kannst du mir sagen, welches Datum wir heute haben?"]
WEEKDAY_DE = ["welcher Wochentag ist heute", "Welchen Wochentag haben wir?"]


@pytest.mark.parametrize("phrase", TIME_EN)
def test_english_time_questions(phrase):
    m = match(phrase)
    assert m is not None and (m.kind, m.lang) == ("time", "en"), phrase


@pytest.mark.parametrize("phrase", TIME_DE)
def test_german_time_questions(phrase):
    m = match(phrase)
    assert m is not None and (m.kind, m.lang) == ("time", "de"), phrase


@pytest.mark.parametrize("phrase", DATE_EN + WEEKDAY_EN + DATE_DE + WEEKDAY_DE)
def test_date_questions_in_both_languages(phrase):
    m = match(phrase)
    expect_weekday = phrase in WEEKDAY_EN + WEEKDAY_DE
    assert m is not None and m.kind == ("weekday" if expect_weekday else "date"), phrase
    assert m.lang == ("de" if phrase in DATE_DE + WEEKDAY_DE else "en")


# ---------------------------------------------------------------- what must NOT be recognised

NOT_RULE_EN = [
    # another time, another place, something else that has a time
    "What time does the shop close?", "what time is the dentist appointment", "What time is my train?", "what time is my next meeting",
    "what time do I have to leave", "What time is dinner at Lea's?", "what time is it in Tokyo", "what time is it in New York",
    "what time was it when I left", "what's the time zone of Berlin", "what is the time complexity of quicksort",
    "what time is it going to be when I get there", "what time will it be in two hours", "what time is it at the finish",
    "what time is it there", "what time is it for Lea", "what is the time of the test", "what time",
    "what day is the exam", "what day is the school trip", "what's the date of the test", "what's the date of my birthday",
    "what day does the library open", "what day should I study for the test", "what date is the exam", "what is the date format in Germany",
    "which day is best to visit Lea", "what day is it in Tokyo", "what day was it yesterday", "what is the date tomorrow",
    "what day is it tomorrow", "what day will it be in 10 days", "what day is Christmas", "what's the date on the invoice",
    # a fragment is not a question
    "the time", "time", "the date", "date", "today", "what is today", "what's next", "next", "hello", "thanks", "good morning",
    "how are you", "what's up", "yes", "okay", "stop", "",
    # other things, same words
    "what's the weather today", "what's on my calendar today", "tell me about my day", "can you read my mail", "what's the plan for today",
    "do you have the time to look at my mail", "do you have the time for a longer question", "can you tell me the time of my train",
    "tell me what time the party starts", "what time is it at the dentist's", "what time is it, and what's the weather",
    "what time is it and how long is my commute", "it's time", "is it time to leave",
    # too long to be sure
    "hey Kairo could you please tell me what time it is right now because I want to know if I can still catch the train",
]
NOT_RULE_DE = [
    "Wie spät ist der Zug?", "Wie spät ist es in Tokio?", "Um wie viel Uhr beginnt die Schule?", "Welcher Tag ist die Prüfung?",
    "Welches Datum hat die Prüfung?", "Wie viel Uhr habe ich noch bis zum Training", "welcher Tag ist der beste für Training",
    "Welcher Tag ist morgen?", "Welches Datum ist morgen", "welches datum haben wir morgen", "wie viel Uhr fährt der Bus",
    "Wie spät kommt der Zug", "Wie spät ist es morgen", "Wie spät ist es bei Lea", "Wie viel kostet das", "Wie spät ist es in Berlin",
    "Welcher Tag ist heute der Test", "Welches Datum hat der Test", "den wievielten hat der Test", "Welcher Tag war gestern",
    "Uhrzeit", "das Datum", "heute", "Wie spät", "Hallo", "Danke", "Guten Morgen", "wie geht's", "ja", "Was steht heute an",
    "wie spät ist es und wie wird das Wetter", "Kannst du mir sagen, wie spät der Zug fährt", "wie viel Uhr ist es in Zürich",
]


@pytest.mark.parametrize("phrase", NOT_RULE_EN + NOT_RULE_DE)
def test_things_that_only_look_like_a_fast_path_question_go_on_to_the_voice_model(phrase):
    assert match(phrase) is None, phrase


def test_the_politeness_and_the_name_are_left_out_but_nothing_else():
    assert norm("Hey Kairo, what's the time, please?") == "what is the time"
    assert norm("Okay, Kairo: Wie spät ist's?") == "wie spät ist es"
    assert norm("  What   day IS it today?!  ") == "what day is it today"


def test_a_huge_transcript_is_not_a_quick_question_and_costs_nothing():
    import time
    for text in ("what time is it " * 2000, "set a timer for ten minutes " + "word " * 5000, "x" * 100_000, "timer " * 3000):
        t0 = time.perf_counter()
        assert match(text) is None
        assert time.perf_counter() - t0 < 0.05, text[:20]


def test_matching_is_fast_enough_to_run_on_every_turn():
    import time
    phrases = TIME_EN + TIME_DE + DATE_EN + DATE_DE + NOT_RULE_EN + NOT_RULE_DE
    t0 = time.perf_counter()
    for _ in range(20):
        for p in phrases:
            match(p)
    per_call_us = (time.perf_counter() - t0) / (20 * len(phrases)) * 1e6
    assert per_call_us < 2000, per_call_us                    # measured about 10 us; the margin is for a busy shared machine


# ---------------------------------------------------------------- the language of the question and of the call

def test_the_language_the_call_is_set_to_limits_what_a_rule_may_answer():
    assert match("what time is it", ("en",)) is not None and match("wie spät ist es", ("en",)) is None
    assert match("wie spät ist es", ("de",)) is not None and match("what time is it", ("de",)) is None
    assert match("wie spät ist es", ("en", "de")) is not None


# ---------------------------------------------------------------- the answers (exact words)

def answer(phrase, now=WED, **kw):
    fp = fastpath.FastPaths(tz=TZ, now=lambda: now)
    rules = fp.rules(time_date=True, **kw)
    got = rules.resolve(phrase)
    return got.text if got else None


@pytest.mark.parametrize("hour,minute,en,de", [
    (14, 35, "It's 2:35 PM.", "Es ist 14 Uhr 35."), (9, 5, "It's 9:05 AM.", "Es ist 9 Uhr 5."), (0, 0, "It's midnight.", "Es ist 0 Uhr."),
    (12, 0, "It's noon.", "Es ist 12 Uhr."), (0, 12, "It's 12:12 AM.", "Es ist 0 Uhr 12."), (15, 0, "It's 3 PM.", "Es ist 15 Uhr."),
    (23, 59, "It's 11:59 PM.", "Es ist 23 Uhr 59."), (12, 30, "It's 12:30 PM.", "Es ist 12 Uhr 30."), (7, 0, "It's 7 AM.", "Es ist 7 Uhr."),
])
def test_the_time_is_said_the_way_each_language_says_it(hour, minute, en, de):
    now = dt.datetime(2026, 10, 14, hour, minute, 30, tzinfo=TZ)
    assert answer("what time is it", now) == en
    assert answer("wie spät ist es", now) == de


def test_the_date_and_the_weekday_in_both_languages_without_a_locale():
    assert answer("what's the date") == "It's Wednesday, October 14, 2026."
    assert answer("welches Datum haben wir heute") == "Heute ist Mittwoch, der 14. Oktober 2026."
    assert answer("what day of the week is it") == "It's Wednesday."
    assert answer("welcher Wochentag ist heute") == "Heute ist Mittwoch."
    assert answer("what day is it", SUN) == "It's Sunday, October 25, 2026."
    assert answer("welcher Tag ist heute", SUN) == "Heute ist Sonntag, der 25. Oktober 2026."
    assert answer("welches datum ist heute", dt.datetime(2026, 3, 1, 8, 0, tzinfo=TZ)) == "Heute ist Sonntag, der 1. März 2026."


def test_the_time_is_the_owners_not_the_servers():
    """The clock is read in the profile's zone, whatever zone the machine runs in."""
    utc = dt.datetime(2026, 10, 14, 12, 35, tzinfo=dt.timezone.utc)               # 14:35 in Zurich (CEST)
    assert answer("what time is it", utc) == "It's 2:35 PM."


# ---------------------------------------------------------------- the switch

def test_with_the_switch_off_nothing_is_answered_by_a_rule():
    fp = fastpath.FastPaths(tz=TZ, now=lambda: WED)
    assert fp.rules(time_date=False).resolve("what time is it") is None
    assert fp.rules(time_date=True).resolve("what time is it") is not None


def test_the_language_of_the_speech_to_text_must_agree_with_the_words():
    """A German sentence that Whisper called English (or the reverse) would be said in the wrong voice: go to the model."""
    rules = fastpath.FastPaths(tz=TZ, now=lambda: WED).rules(time_date=True)
    assert rules.resolve("wie spät ist es", language="de") is not None
    assert rules.resolve("wie spät ist es", language="en") is None
    assert rules.resolve("what time is it", language="de") is None
    assert rules.resolve("what time is it", language="en") is not None
    assert rules.resolve("what time is it", language=None) is not None            # not known: the words decide
    assert rules.resolve("wie spät ist es", mode="en") is None                    # a call set to English only
    assert rules.resolve("what time is it", mode="de") is None                    # German only
    assert rules.resolve("what time is it", mode="auto", language="en") is not None


def test_a_rule_that_breaks_is_no_answer_and_never_an_error():
    def boom():
        raise RuntimeError("clock broke")
    assert fastpath.FastPaths(tz=TZ, now=boom).rules(time_date=True).resolve("what time is it") is None


# ---------------------------------------------------------------- the owner's switch, read from the database

def test_g10_fast_paths_is_on_by_default_and_the_owner_can_turn_it_off(migrated):
    from sqlmodel import Session

    from app import flags
    from app.db import make_engine
    engine = make_engine(migrated.db_url)
    fp = fastpath.FastPaths(tz=TZ, now=lambda: WED, engine=engine)
    assert flags.FLAGS["g10.fast_paths"][0] is True
    assert fp.for_call().resolve("what time is it").text == "It's 2:35 PM."          # the default: on
    with Session(engine) as s:
        flags.set_flag(s, "g10.fast_paths", False)
    assert fp.for_call().resolve("what time is it") is None                           # off: the voice model answers
    assert fp.for_call().resolve("wie spät ist es") is None
    with Session(engine) as s:
        flags.set_flag(s, "g10.fast_paths", True)
    assert fp.for_call().resolve("wie spät ist es").text == "Es ist 14 Uhr 35."


def test_a_call_keeps_the_switches_it_started_with(migrated):
    from sqlmodel import Session

    from app import flags
    from app.db import make_engine
    engine = make_engine(migrated.db_url)
    fp = fastpath.FastPaths(tz=TZ, now=lambda: WED, engine=engine)
    rules = fp.for_call()
    with Session(engine) as s:
        flags.set_flag(s, "g10.fast_paths", False)
    assert rules.resolve("what time is it") is not None and fp.for_call().resolve("what time is it") is None


def test_random_word_salad_never_raises_and_never_takes_long():
    """5,000 random strings of the words the rules use (a fixed seed): no error, and none takes more than a tenth of a second."""
    import random
    import time
    vocab = ("set a an the timer timers for of on minutes minute hours hour seconds ten five 10 5 1.5 and half quarter remind me in to about "
             "cancel stop what time is it day date my next train bus lesson class appointment when does leave Lea nudeln stell einen Timer "
             "auf Minuten Stunde eine halbe erinner mich an die Wäsche wann fährt der nächste Zug Termin wie spät ist es kairo please bitte "
             "can you tell me how long left much on").split()
    rnd = random.Random(1)
    worst = 0.0
    for _ in range(5000):
        s = " ".join(rnd.choice(vocab) for _ in range(rnd.randint(1, 20)))
        t0 = time.perf_counter()
        match(s)
        worst = max(worst, time.perf_counter() - t0)
    assert worst < 0.1, worst
