"""What is said on a call is plain speech (owner's call 2026-10-20: he heard "The owner mentioned…",
"L23 (urgent, loop)", "Let me check the bring-up list" and numbered dumps). speech.plain() is the one
sanitizer for every text spoken or shown on a call; PlainStream applies it to words that arrive in pieces
without holding back the first words of a normal sentence. Made-up examples only."""

from __future__ import annotations

import pytest

from app.speech import PlainStream, plain


@pytest.mark.parametrize("raw, want", [
    ("L23 (urgent, loop): you still owe Lea an answer about Friday.", "You still owe Lea an answer about Friday."),
    ("Your card #12 is waiting in Approvals.", "Your card is waiting in Approvals."),
    ("Lea's question (K4) is still open.", "Lea's question is still open."),
    ("Loop 23 is about the phone case.", "Loop is about the phone case."),
    ("The phone case (normal, loop) is still open (L7).", "The phone case is still open."),
    ("Q5: is the blue jacket a gift?", "Is the blue jacket a gift?"),
])
def test_internal_ids_and_tags_are_never_said(raw, want):
    assert plain(raw) == want


@pytest.mark.parametrize("raw, want", [
    ("Let me check the bring-up list. You have training at 18:00.", "You have training at 18:00."),
    ("Okay, let me look at that. Training is at 18:00.", "Training is at 18:00."),
    ("Sure, let me check that. Training is at 18:00.", "Training is at 18:00."),
    ("I'll look at the open loops list first. Lea is waiting for your answer.", "Lea is waiting for your answer."),
    ("The owner mentioned a dentist visit. Your dentist is on Friday at 9.", "Your dentist is on Friday at 9."),
    ("According to the bring-up list, you should sign the form. Training is at 6.", "Training is at 6."),
    ("Your mail is all answered. The owner's train is at 7:12.", "Your mail is all answered."),
])
def test_thinking_out_loud_and_notes_about_the_owner_are_dropped(raw, want):
    assert plain(raw) == want


def test_a_numbered_dump_becomes_at_most_two_short_sentences():
    raw = ("Here's what's open:\n1. L12 (urgent, loop): reply to Lea about Friday\n2. Order the phone case\n"
           "3. Sign the school form\n4. Return the library book")
    out = plain(raw)
    assert out == "Here's what's open: reply to Lea about Friday, Order the phone case."
    assert plain("- Reply to Lea\n- Order the phone case\n- Sign the form") == "Reply to Lea. Order the phone case."
    assert plain("Three things: 1. Reply to Lea 2. Order the case 3. Sign the form.") == \
        "Three things: Reply to Lea, Order the case."
    assert plain("* **Training** at 18:00\n* Dentist on Friday") == "Training at 18:00. Dentist on Friday."


def test_never_empty_when_there_was_something_to_say():
    assert plain("The owner has a dentist appointment at 3.") == "You have a dentist appointment at 3."
    assert plain("1. Let me check\n2. Training at 6") == "Training at 6."
    assert plain("Let me check the bring-up list.") == ""                  # only thinking out loud: nothing to say
    assert plain("  ") == "" and plain("L23") == ""


@pytest.mark.parametrize("fine", [
    "Your train leaves at 7:12 from platform 2.",
    "Lea said yes for Friday, and the Q4 report is due next week.",
    "Am 3. Oktober hast du die Prüfung.",
    "Let me know if you want me to add it.",
    "From your card: Home: now 12°C, cloudy",
    "echo: (spoken, on a call) Remind me to call the dentist tomorrow",
    "Sure, I'll remind you tomorrow at 9.",
    "Training ends at 19:30, so you'll be home by 8.",
    "I'm going to add milk to your shopping list.",
    "I'll check with Lea and get back to you.",
    "Your report is due in Q4.",
    "Question 3 on the test was about fractions.",
])
def test_plain_speech_is_left_alone(fine):
    assert plain(fine) == fine


def test_the_owner_never_appears_in_what_is_said():
    assert plain("I sent it to Lea for the owner.") == "I sent it to Lea for you."
    assert "owner" not in plain("Okay. The owner wants milk. You have a test on Friday.").lower()


def test_special_tokens_and_markers_are_never_said():
    assert plain("Okay.<|im_end|>") == "Okay."
    assert plain("<tool_call>{}</tool_call> Sure.") == "Sure."


# ---------------------------------------------------------------- in pieces, as the voice model writes

def feed_all(pieces) -> tuple[list[str], str]:
    ps = PlainStream()
    outs = [ps.feed(p) for p in pieces]
    outs.append(ps.flush())
    return outs, "".join(outs)


def test_the_first_words_of_a_normal_sentence_go_out_at_once():
    ps = PlainStream()
    assert ps.feed("Sure, ") == "Sure, "                   # a cached opener: the voice starts now
    assert ps.feed("your ") == ""                          # one word: could still be "the owner ..."
    assert ps.feed("train lea") == "your train "           # two words decide it; a word still growing is kept back
    assert ps.feed("ves at ") == "leaves at "
    assert ps.feed("7:12.") + ps.flush() == "7:12."
    ps = PlainStream()
    assert ps.feed("echo: ") == "echo: "                   # the first clause, like the voice's first piece


def test_a_meta_sentence_is_held_and_dropped_never_half_said():
    outs, said = feed_all(["Let me ", "check the ", "bring-up list. ", "You have ", "training at 6."])
    assert said == "You have training at 6." and outs[0] == "" and outs[1] == ""
    outs, said = feed_all(["Okay so the ", "owner wants ", "milk. ", "Milk is on your list."])
    assert said == "Milk is on your list." and "owner" not in said


def test_ids_and_tags_split_across_pieces_are_removed():
    _, said = feed_all(["Quick one: L", "23 (urgent", ", loop) is ", "the school form."])
    assert said == "Quick one: is the school form." and "L" not in said.replace("Quick", "")
    _, said = feed_all(["Your card #", "12 is waiting."])
    assert said == "Your card is waiting."


def test_a_list_in_pieces_is_said_as_two_sentences():
    _, said = feed_all(["Here's what's open:\n", "1. Reply to Lea\n", "2. Order the case\n", "3. Sign the form"])
    assert said == "Here's what's open: Reply to Lea. Order the case."


def test_plain_words_in_pieces_come_out_unchanged():
    text = "Sure, your train leaves at 7:12 from platform 2. Training ends at 19:30, so you'll be home by 8."
    for size in (1, 3, 7):
        _, said = feed_all([text[i:i + size] for i in range(0, len(text), size)])
        assert said == text, size
