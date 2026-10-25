"""G10: reading a timer sentence (app/timerphrase.py). Only a sentence that is exactly "set a timer for <length> [for
<label>]" (or the reminder / question / cancel form of it) is read; a length that is not a clear length ("ten", "a while",
"until three") or words that are left over mean no match, and the voice model gets the sentence as before. English and
German, with the numbers as digits and as words, the way Whisper writes them."""

from __future__ import annotations

import pytest

from app import timerphrase
from app.fastpath import match

MIN, HOUR = 60, 3600


def got(text, langs=("en", "de")):
    m = match(text, langs)
    return None if m is None else (m.kind, m.lang, m.data)


def setting(text):
    m = match(text)
    assert m is not None and m.kind == "timer_set", text
    return m.data["seconds"], m.data.get("label"), bool(m.data.get("reminder")), m.lang


# ---------------------------------------------------------------- setting a timer

SET_EN = [
    ("set a timer for 10 minutes", 10 * MIN, None), ("Set a timer for ten minutes", 10 * MIN, None), ("timer for 5 minutes", 5 * MIN, None),
    ("Kairo, start a timer for 30 seconds", 30, None), ("set a timer for an hour", HOUR, None), ("set a timer for half an hour", 30 * MIN, None),
    ("set a 20 minute timer", 20 * MIN, None), ("set a timer for one hour and thirty minutes", 90 * MIN, None),
    ("set a timer for 1 hour 15 minutes", 75 * MIN, None), ("make a timer for 45 seconds", 45, None), ("start a timer for two minutes", 2 * MIN, None),
    ("set a timer for a quarter of an hour", 15 * MIN, None), ("set a timer for an hour and a half", 90 * MIN, None),
    ("timer 10 minutes", 10 * MIN, None), ("Can you set a timer for 15 minutes?", 15 * MIN, None), ("please set a timer for 3 minutes", 3 * MIN, None),
    ("set a timer for 2 and a half minutes", 150, None), ("set a timer for one and a half hours", 90 * MIN, None), ("set a timer for 1.5 hours", 90 * MIN, None),
    ("set a timer for twenty five minutes", 25 * MIN, None), ("set a timer for forty-five minutes", 45 * MIN, None), ("set the timer for 7 minutes", 7 * MIN, None),
    ("set a timer for 90 seconds", 90, None), ("start a timer for a minute", MIN, None), ("set a timer for three quarters of an hour", 45 * MIN, None),
    ("set a ten minute timer", 10 * MIN, None), ("a 10 minute timer", 10 * MIN, None), ("set a timer for 2 hrs", 2 * HOUR, None),
    ("set a timer for 5 mins", 5 * MIN, None), ("set a timer for 20 secs", 20, None), ("I want a timer for 10 minutes", 10 * MIN, None),
    ("Set a timer for ten minutes, please.", 10 * MIN, None), ("set up a timer for 3 minutes", 3 * MIN, None), ("set a timer for 3 minutes 30 seconds", 210, None),
    ("set a timer for three and a half minutes", 210, None), ("set a timer for ninety minutes", 90 * MIN, None), ("set a timer for one minute thirty seconds", 90, None),
    ("set a timer for 10 minutes for tea", 10 * MIN, "tea"),
    ("set a timer for 10 minutes for the pasta", 10 * MIN, "the pasta"), ("set a timer for ten minutes called eggs", 10 * MIN, "eggs"),
    ("set a timer for 20 minutes to check the oven", 20 * MIN, "check the oven"), ("start a 5 minute timer for the tea", 5 * MIN, "the tea"),
]
SET_DE = [
    ("Stell einen Timer auf 10 Minuten", 10 * MIN, None), ("stell einen Timer auf zehn Minuten", 10 * MIN, None),
    ("Stelle bitte einen Timer auf eine halbe Stunde", 30 * MIN, None), ("Timer auf 5 Minuten", 5 * MIN, None),
    ("starte einen Timer für 90 Sekunden", 90, None), ("stell einen Timer auf anderthalb Stunden", 90 * MIN, None),
    ("Stell einen Timer auf eine Stunde", HOUR, None), ("mach einen Timer für 15 Minuten", 15 * MIN, None),
    ("stell einen Timer auf eine Viertelstunde", 15 * MIN, None), ("setze einen Timer auf zwei Stunden", 2 * HOUR, None),
    ("Timer zehn Minuten", 10 * MIN, None), ("Kannst du einen Timer auf 20 Minuten stellen?", 20 * MIN, None),
    ("Kairo, stell mir mal einen Timer auf fünfundzwanzig Minuten", 25 * MIN, None), ("stell einen Timer auf eine Dreiviertelstunde", 45 * MIN, None),
    ("stell einen Timer auf zweieinhalb Minuten", 150, None), ("stell einen Timer auf 1,5 Stunden", 90 * MIN, None),
    ("stell einen Timer auf eine Stunde und 15 Minuten", 75 * MIN, None), ("starte einen Timer für dreißig Sekunden", 30, None),
    ("stell einen Timer auf 8 Min", 8 * MIN, None), ("stell einen zehn Minuten Timer", 10 * MIN, None), ("Ich brauche einen Timer für 10 Minuten", 10 * MIN, None),
    ("stell einen Timer auf 3 Minuten und 30 Sekunden", 210, None), ("Stell den Timer auf 5 Minuten", 5 * MIN, None), ("Timer für zehn Minuten", 10 * MIN, None),
    ("Timer auf zwei Stunden und dreißig Minuten", 150 * MIN, None), ("mach einen Timer auf 5 Minuten für Tee", 5 * MIN, "Tee"),
    ("Stell einen Timer auf 10 Minuten für die Nudeln", 10 * MIN, "die Nudeln"), ("stell einen Timer auf zehn Minuten namens Eier", 10 * MIN, "Eier"),
]


@pytest.mark.parametrize("text,seconds,label", SET_EN)
def test_english_timer_sentences(text, seconds, label):
    s, lab, reminder, lang = setting(text)
    assert (s, lab, reminder, lang) == (seconds, label, False, "en"), text


@pytest.mark.parametrize("text,seconds,label", SET_DE)
def test_german_timer_sentences(text, seconds, label):
    s, lab, reminder, lang = setting(text)
    assert (s, lab, reminder, lang) == (seconds, label, False, "de"), text


REMIND = [
    ("remind me in 20 minutes to call Lea", 20 * MIN, "call Lea", "en"), ("remind me in an hour", HOUR, None, "en"),
    ("remind me in ten minutes about the oven", 10 * MIN, "the oven", "en"), ("Kairo remind me in 5 minutes", 5 * MIN, None, "en"),
    ("remind me in half an hour to stretch", 30 * MIN, "stretch", "en"), ("remind me in two hours that the parcel comes", 2 * HOUR, "the parcel comes", "en"),
    ("set a reminder in 10 minutes to stir", 10 * MIN, "stir", "en"),
    ("Erinner mich in 20 Minuten an die Wäsche", 20 * MIN, "an die Wäsche", "de"), ("erinner mich in einer Stunde", HOUR, None, "de"),
    ("erinnere mich in 5 Minuten", 5 * MIN, None, "de"), ("erinner mich in einer halben Stunde an den Ofen", 30 * MIN, "an den Ofen", "de"),
    ("erinner mich in zehn Minuten daran, Lea anzurufen", 10 * MIN, "Lea anzurufen", "de"), ("erinner mich bitte in 15 Minuten", 15 * MIN, None, "de"),
    ("Kannst du mich in 10 Minuten erinnern", 10 * MIN, None, "de"),
]


@pytest.mark.parametrize("text,seconds,label,lang", REMIND)
def test_reminders_are_timers_with_the_owners_words(text, seconds, label, lang):
    s, lab, reminder, got_lang = setting(text)
    assert (s, lab, reminder, got_lang) == (seconds, label, True, lang), text


# ---------------------------------------------------------------- asking about and cancelling

def test_asking_how_long_is_left_and_which_timers_there_are():
    left = ["how much time is left on my timer", "how long is left on the timer", "How much time is left on the timer?", "how much longer on my timer",
            "how long until my timer goes off", "when does my timer go off", "how much time does my timer have left", "what is left on my timer",
            "wie lange läuft der Timer noch", "Wie viel Zeit ist noch auf dem Timer?", "wie lange noch bis mein Timer klingelt", "wann ist der Timer fertig",
            "wie viel Zeit bleibt noch auf dem Timer", "wie viel Zeit ist noch auf meinem Timer", "wie lange läuft mein Timer noch"]
    for t in left:
        assert got(t) is not None and got(t)[0] == "timer_left", t
    lst = ["which timers do I have", "what timers are running", "do I have a timer running", "are there any timers running", "list my timers",
           "how many timers do I have", "welche Timer habe ich", "läuft ein Timer", "Habe ich einen Timer laufen", "welche Timer laufen gerade",
           "wie viele Timer habe ich", "zeig mir meine Timer", "what timers are running right now", "do I have any timers running right now"]
    for t in lst:
        assert got(t) is not None and got(t)[0] == "timer_list", t


def test_cancelling_names_one_timer_all_of_them_or_the_only_one():
    plain = ["cancel the timer", "stop the timer", "cancel timer", "Kairo cancel my timer", "delete the timer", "turn off the timer", "Timer abbrechen",
             "den Timer abbrechen", "brich den Timer ab", "lösch den Timer", "stopp den Timer", "Timer stoppen", "beende den Timer", "Stoppe bitte den Timer"]
    for t in plain:
        m = got(t)
        assert m is not None and m[0] == "timer_cancel" and not m[2].get("all") and not m[2].get("q"), t
    every = ["cancel all timers", "stop all the timers", "cancel my timers", "delete all my timers", "alle Timer abbrechen", "lösche alle Timer", "stoppe alle Timer"]
    for t in every:
        m = got(t)
        assert m is not None and m[0] == "timer_cancel" and m[2].get("all"), t
    one = {"cancel the pasta timer": ("pasta", None), "stop the 10 minute timer": (None, 600), "cancel the ten minute timer": (None, 600),
           "den Nudel Timer abbrechen": ("nudel", None), "den Timer für die Nudeln abbrechen": ("nudeln", None), "stop the eggs timer": ("eggs", None), "lösche den Timer für 5 Minuten": (None, 300), "cancel the timer for the eggs": ("eggs", None)}
    for t, (q, secs) in one.items():
        m = got(t)
        assert m is not None and m[0] == "timer_cancel", t
        assert m[2].get("q") == q and m[2].get("seconds") == secs, (t, m)


# ---------------------------------------------------------------- what is NOT a timer sentence

NOT_TIMER = [
    # no clear length
    "set a timer for the pasta", "set a timer", "stell einen Timer", "set a timer for 10", "set a timer for ten", "remind me in a while", "remind me in a bit",
    "remind me in 5", "set a timer until three", "set a timer for 10 minutes at 5pm", "set a timer for a million minutes", "Timer", "timer for", "stell einen Timer auf",
    "set a timer for zero minutes", "set a timer for 5 minutes to 10 minutes", "set a timer for 1 hour 30", "how much time is left", "stell einen Timer auf 0 Minuten", "set a timer for 10 days", "erinner mich in zwei Tagen", "remind me in 3 weeks",
    # a time of day, a date: not a length
    "remind me tomorrow at five", "remind me at 5 pm to call Lea", "erinner mich morgen um 8", "erinner mich heute an die Wäsche", "wake me up at seven",
    "set an alarm for 7 in the morning", "set an alarm for seven", "Stell den Wecker auf 7 Uhr", "stell den Wecker auf zehn Minuten", "remind me on Friday",
    # a length, but not a timer
    "in 10 minutes the train leaves", "the train leaves in ten minutes", "how long does it take to boil an egg", "wie lange dauert es bis zum Bahnhof",
    "I need ten minutes to get ready", "give me ten minutes", "dauert zehn Minuten", "schick mir in zehn Minuten eine Nachricht", "text Lea in ten minutes",
    "call me in ten minutes", "email Lea in an hour", "put a timer on the oven", "the timer is broken", "my timer didn't ring", "mein Timer hat nicht geklingelt",
    "is the timer loud", "what is a timer", "wie funktioniert der Timer", "set a timer for 10 minutes then call Lea", "set a timer for 10 minutes and then another for 5",
    "set a timer for 10 minutes and remind me to stir", "set a timer for 10 minutes for the pasta and also one for the eggs", "set a timer for 5 minutes tomorrow",
    "set a timer for 20 minutes to be at the station at 5", "remind me in 20 minutes to be there by 5pm", "set a timer for 10 minutes at the gym",
    # remind without a length
    "remind me what the plan is", "remind me to buy milk", "erinner mich an den Termin", "remind me to call Lea in an hour",
    # questions and cancels about other things
    "how long is left", "how long is the timer", "how much time is left on the lesson", "when does the lesson end", "how long until the train leaves",
    "cancel the meeting", "cancel my subscription", "stop the music", "delete the note", "cancel the appointment", "stop", "cancel", "stop the timer from beeping",
    "Timer ist kaputt", "den Termin abbrechen", "die Musik stoppen", "lösche die Notiz", "cancel the timer on the oven", "welche Termine habe ich",
    "which timers should I use", "how many minutes in an hour", "add five minutes to the timer", "pause the timer", "wie viele Minuten hat eine Stunde",
    "hello", "thanks Kairo", "good morning",
]


@pytest.mark.parametrize("text", NOT_TIMER)
def test_what_is_not_a_clear_timer_sentence_goes_on_to_the_voice_model(text):
    m = match(text)
    assert m is None or not m.kind.startswith("timer"), (text, m)


def test_a_label_may_not_be_a_time_of_day_or_a_pile_of_words():
    assert match("set a timer for 10 minutes for the 2 o'clock meeting") is None
    assert match("remind me in 10 minutes to call Lea and then Marc and then Anna and then the school office") is None
    m = match("set a timer for 10 minutes for the second batch")
    assert m is not None and m.data["label"] == "the second batch"


def test_a_name_to_cancel_is_a_few_words_not_a_sentence():
    assert got("cancel the pasta timer") is not None and got("cancel the big green pasta timer") is not None
    for junk in ("stop and ten Timer", "stop long nudeln appointment of class date Timer", "cancel much and day timers",
                 "stop der time day class erinner next hours timer", "cancel when a 10 ist eine remind is leave timer"):
        assert got(junk) is None, junk


def test_the_length_is_a_whole_number_of_seconds():
    assert setting("set a timer for 1.5 minutes")[0] == 90
    assert setting("set a timer for 0.5 hours")[0] == 1800
    assert match("set a timer for 0.2 seconds") is None                              # under a second: not a timer
    assert setting("set a timer for 30 hours")[0] == 30 * HOUR                        # read; setting it says "up to a day"


def test_parsing_is_fast_and_never_slow_on_odd_input():
    import time
    phrases = [t for t, *_ in SET_EN + SET_DE + REMIND] + NOT_TIMER + ["timer " * 200, "set a timer for " + "ten " * 300, "remind me in " + "x " * 500]
    t0 = time.perf_counter()
    for _ in range(5):
        for p in phrases:
            match(p)
    per = (time.perf_counter() - t0) / (5 * len(phrases)) * 1e6
    assert per < 3000, per                                                           # about 20 us measured; a busy shared machine gets room
    assert timerphrase.MAX_WORDS <= 20
