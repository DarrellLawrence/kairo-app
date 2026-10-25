"""Who may interrupt Kairo while it speaks (app/bargein.py, voice plan section 1, group G6): pure text rules.

The phrase tables are written by hand (made-up, no personal data). They test coverage of the word lists, not the rate on
the owner's real mouth: the lists were tuned on phrases like these, so a pass here is necessary, not sufficient. The
rate on the owner's real Whisper output is measurement P10 on the PC.
"""

from __future__ import annotations

import random

import pytest

from app import bargein
from app.bargein import BACKCHANNEL, STOP_WORDS, barge_in, echo, interrupts, words

BACK_EN = ["Mhm.", "Mm-hmm.", "Uh-huh.", "Yeah.", "Yeah, yeah.", "Okay.", "Okay, okay.", "Right.", "Right, right.", "I see.",
           "Got it.", "Oh, okay.", "Oh, I see.", "Sure.", "Cool.", "Nice.", "Yes.", "Yep.", "Yeah, right.", "Oh yeah.",
           "Ah, okay.", "Okay, cool.", "Okay, got it.", "Alright.", "Gotcha.", "Interesting.", "Oh nice.", "That's right.",
           "Exactly.", "Of course.", "I know.", "Oh really?", "Thank you.", "Thanks.", "Okay, thanks.", "Oh wow.",
           "Yeah, that makes sense.", "Sounds good.", "Um-hmm.", "Mmm-hmm."]
BACK_DE = ["Ja.", "Ja, ja.", "Genau.", "Alles klar.", "Achso.", "Ach so.", "Okay.", "Stimmt.", "Super.", "Gut.", "Ja, genau.",
           "Ja, klar.", "Mhm.", "Aha.", "Ah, okay.", "Okay, gut.", "Ja, gut.", "Na gut.", "Alles gut.", "Verstehe.", "Ach ja.",
           "Ach so, okay.", "Danke.", "Okay, danke.", "Ja, danke.", "Echt?", "Ah ja.", "Ja, stimmt.", "Klar.", "Natürlich.",
           "Ach was.", "Das stimmt.", "Ja, das stimmt.", "Alles klar, danke.", "Sehr gut.", "Passt."]
REAL_EN = ["No wait, Tuesday.", "Stop.", "Wait.", "Hold on.", "Hold on a second.", "Actually, make it five.", "No, not that one.",
           "Sorry, what?", "What did you say?", "Cancel that.", "No.", "Wait, wait.", "Pause.", "Quiet.", "Enough.",
           "Never mind.", "That's wrong.", "Hang on.", "Sorry.", "Hey Kairo.", "Kairo, stop.", "One second.", "No, no, no.",
           "Can you repeat that?", "Louder please.", "Slow down.", "Skip that.", "Not now.", "I have a question.",
           "Let me say something."]
REAL_DE = ["Nein, warte.", "Stopp.", "Warte.", "Halt.", "Moment.", "Moment mal.", "Eigentlich, mach fünf.", "Nein, nicht das.",
           "Wie bitte?", "Was hast du gesagt?", "Vergiss das.", "Nein.", "Pause.", "Ruhe.", "Genug.", "Lass mal.",
           "Das ist falsch.", "Sekunde.", "Entschuldigung.", "Hey Kairo.", "Kairo, stopp.", "Nicht jetzt.",
           "Kannst du das wiederholen?", "Langsamer bitte.", "Ich habe eine Frage.", "Lass mich was sagen.",
           "Nein, nein, nein.", "Halt mal an."]


@pytest.mark.parametrize("text", BACK_EN + BACK_DE)
def test_a_sound_that_means_go_on_does_not_interrupt(text):
    assert barge_in(text) is False, text


@pytest.mark.parametrize("text", REAL_EN + REAL_DE)
def test_a_stop_word_or_real_words_interrupt(text):
    assert barge_in(text) is True, text


def test_single_words_interrupt_only_when_they_are_stop_words():
    for w in STOP_WORDS:
        assert barge_in(w.capitalize() + ".") is True, w
    for w in ("Mhm.", "Okay.", "Yeah.", "Ja.", "Genau.", "Kairo.", "Tuesday.", "Thanks.", "Hello."):
        assert barge_in(w) is False, w                            # one word that is not a stop word never interrupts
    assert barge_in("") is False and barge_in("   ") is False and barge_in(None) is False


def test_the_lists_do_not_overlap_and_a_stop_word_is_never_a_backchannel():
    assert not (STOP_WORDS & BACKCHANNEL), STOP_WORDS & BACKCHANNEL
    assert bargein.BARGE_IN_WORDS == 2


def test_two_words_that_are_not_all_backchannel_interrupt():
    assert barge_in("make it five") is True
    assert barge_in("okay but") is True
    assert barge_in("okay okay okay") is False
    assert barge_in("ja genau klar") is False


def test_words_are_split_the_way_whisper_writes_them():
    assert words("Mm-hmm.") == ["mm", "hmm"]
    assert words("That’s right!") == ["that's", "right"]            # typographic apostrophe
    assert words("Natürlich, Müller-Straße?") == ["natürlich", "müller", "straße"]
    assert words("...") == [] and words(None) == []
    assert barge_in("That’s right.") is False


def test_what_whisper_makes_of_noise_does_not_interrupt():
    """Measured: Whisper with its voice filter on wrote '' or 'you' for non-speech (research section 3)."""
    for text in ("", "you", "You", "Thank you.", "Bye."):
        assert barge_in(text) is False, text


# ---------------------------------------------------------------- Kairo's own voice coming back

SAID = ["Your next meeting is at three with the design team.", "I added milk to the shopping list.",
        "It will be sunny tomorrow, around eighteen degrees.", "Do you want me to move the dentist appointment to Thursday?",
        "I can't find anything about that in your notes.", "Wait a moment while I check your calendar.",
        "Should I stop the timer now?", "The train to Zurich leaves at eight fifteen, platform four.",
        "Das Treffen mit dem Team ist um drei Uhr.", "Ich habe Milch auf die Einkaufsliste gesetzt.",
        "Morgen wird es sonnig, etwa achtzehn Grad.", "Soll ich den Termin beim Zahnarzt auf Donnerstag verschieben?"]


def test_kairos_own_words_coming_back_are_an_echo_whatever_part_of_the_line_it_was():
    for line in SAID:
        w = line.split()
        for k in (3, 5, len(w)):
            for start in (0, len(w) - k):
                assert echo(" ".join(w[start:start + k]), [line, "something else entirely"]) is True, (line, k, start)


def test_other_words_are_not_an_echo_and_a_short_text_never_is():
    assert echo("No wait Tuesday please", SAID[:3]) is False
    assert echo("Make it five o'clock", SAID[:3]) is False
    assert echo("", SAID) is False and echo("   ", SAID) is False
    # one or two words are never judged: "Stop." right after Kairo asked 'Should I stop the timer now?' stops it
    assert echo("Stop.", ["Should I stop the timer now?"]) is False
    assert echo("stop the", ["Should I stop the timer now?"]) is False
    assert interrupts("Stop.", ["Should I stop the timer now?"]) is True
    assert echo("stop the timer", ["Should I stop the timer now?"]) is True      # three words, all Kairo's


def test_an_echo_is_not_an_interruption_and_the_owner_over_other_lines_is():
    line = "Wait a moment while I check your calendar."
    assert barge_in("wait a moment while I check") is True            # a stop word, but it is Kairo's own sentence
    assert interrupts("wait a moment while I check", [line]) is False
    assert interrupts("Moment, I meant Friday", [line]) is True       # the owner's own words
    assert interrupts("Mhm.", [line]) is False
    assert interrupts("No wait, Tuesday.", SAID[:3]) is True


def test_real_interruptions_are_rarely_taken_for_an_echo():
    """The research: 2.0 % English, 0.6 % German of real interruptions blocked by lines of Kairo's it happened to share
    words with. Seeded, 3000 draws from the made-up lines above (the same language for the owner and Kairo)."""
    rnd = random.Random(11)
    for real, pool in ((REAL_EN, SAID[:8]), (REAL_DE, SAID[8:])):
        blocked = 0
        for _ in range(3000):
            said = rnd.sample(pool, min(3, len(pool)))
            blocked += not interrupts(rnd.choice(real), said)
        assert blocked / 3000 <= 0.03, (blocked, real[:2])


# ---------------------------------------------------------------- which of Kairo's lines can the microphone hear now

def test_the_lines_near_the_audio_being_played_are_what_an_echo_can_be():
    """Kairo's voice is made faster than it is played: six lines are given to the pipeline within a moment and the
    first one is still playing. The last lines made are then the end of the answer, not what the microphone hears."""
    said = bargein.Said()
    for i, seconds in enumerate((3.0, 3.0, 3.0, 3.0, 3.0, 3.0)):
        said.add(f"line {i}", seconds)
    assert said.near(1.0) == ["line 0"]                                # playing line 0
    assert said.near(2.5) == ["line 0", "line 1"]                      # line 1 starts within the margin
    assert said.near(7.0) == ["line 0", "line 1", "line 2"]            # 1 s to 8 s into the run: nothing of lines 3 to 5
    assert said.near(17.0) == ["line 3", "line 4", "line 5"]
    assert said.near(None) == ["line 3", "line 4", "line 5"]           # the voice stopped: the last three
    assert said.near(500.0) == []                                      # long after: nothing it could be


def test_a_new_run_starts_after_the_voice_stopped_and_the_old_lines_stay_until_then():
    said = bargein.Said()
    said.add("first answer", 2.0)
    said.stopped()
    assert said.near(None) == ["first answer"]                         # words still being written down are judged against it
    said.add("second answer", 2.0)
    assert said.near(0.5) == ["second answer"] and said.total == 2.0   # a new clock
    assert [t for _, _, t in said.lines] == ["second answer"]
    for i in range(100):
        said.add(f"x{i}", 0.1)
    assert len(said.lines) == bargein.Said.KEEP


def test_a_zero_length_line_is_kept_and_a_negative_one_is_not_a_time_machine():
    said = bargein.Said()
    said.add("cached opener", 0.0)
    said.add("next", -1.0)
    assert said.total == 0.0 and said.near(0.0) == ["cached opener", "next"]
