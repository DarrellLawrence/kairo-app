"""Who may interrupt Kairo while it is speaking (voice plan section 1, group G6): pure functions on text, no network, no
audio. Used by voicertc.py's turn start strategy and its speech to text, tested in test_bargein.py.

While Kairo talks, a sound from the owner's side opens a turn but does not stop Kairo. Whisper writes it down, and only
then these two questions decide:

  barge_in(text)  does it sound like the owner wants Kairo to stop or wants to say something? A stop word ("stop",
                  "nein", "moment" ...) does; so do two words or more, unless all of them are the little sounds people
                  make to say "go on" ("mhm", "ja", "genau", "okay", "thank you" ...).
  echo(text, said)  is it Kairo's own voice coming back through the phone's microphone? Then it is not the owner.

The word lists are the research's (voice-recipe.md section 1), extended with the words behind the false interrupts it
measured. They were tuned on phrases written by the researcher, not on the owner's real Whisper output: a coverage
test. P10 measures them on the real thing.
"""

from __future__ import annotations

import re
from typing import Iterable

# One word is enough to stop Kairo. The first five are the research's; the rest are the single-word interruptions its
# rule missed (English and German).
STOP_WORDS = frozenset(
    "stop stopp wait warte halt "
    "no nein moment sekunde pause quiet ruhe enough genug sorry entschuldigung".split())

# Little sounds that mean "go on", not "let me talk". A phrase made only of these (two words or more) is not an
# interruption. A single word never is, unless it is a stop word, so single words only matter inside a phrase.
BACKCHANNEL = frozenset(
    "mhm mm mmm hm hmm um uh huh uhhuh aha ah oh yeah yes yep ok okay right sure cool nice got it i see "
    "ja jaja jo genau gut klar alles achso ach so stimmt super okey "
    # extended: the tokens behind the 17 false interrupts the research measured, and the other words of the same phrases
    "thank thanks you of course really that's that sounds good makes sense know wow exactly alright gotcha "
    "danke das sehr na was natürlich passt verstehe echt".split())

# Fewer words than this can't be told from Kairo's own words: a one- or two-word text is never called an echo (the
# owner's "Stop." right after Kairo said "should I stop the timer?" must stop it).
ECHO_MIN_WORDS = 3
ECHO_SHARE = 0.8             # this share of the words found in what Kairo just said = its own voice
BARGE_IN_WORDS = 2


def words(text: str) -> list[str]:
    """The words of a transcript, lower case, without punctuation. "Mm-hmm." is two words: mm, hmm."""
    return re.findall(r"[\w']+", str(text or "").lower().replace("’", "'").replace("_", " "))


def barge_in(text: str) -> bool:
    """True if this is the owner wanting Kairo to stop or wanting to say something: a stop word, or at least two
    words that are not all backchannel."""
    w = words(text)
    if any(x in STOP_WORDS for x in w):
        return True
    return len(w) >= BARGE_IN_WORDS and not all(x in BACKCHANNEL for x in w)


def echo(text: str, said: Iterable[str]) -> bool:
    """True if at least ECHO_SHARE of the words are words Kairo just said (its last lines): its own voice coming back."""
    w = words(text)
    if len(w) < ECHO_MIN_WORDS:
        return False
    heard = set(words(" ".join(said)))
    return sum(x in heard for x in w) / len(w) >= ECHO_SHARE


class Said:
    """What Kairo's voice said in the run now playing, and where in its audio each line sits. The voice is made faster than
    it is played: a long answer is all spoken to the pipeline in a moment while its first lines are still playing, so
    'the last three lines made' are the end of the answer, not what the microphone can be hearing now. The lines near the
    audio position being played are what its echo can be."""

    LAST = 3                 # lines kept when the voice has stopped (or its position is not known)
    BEHIND_S = 6.0           # a stretch of the owner's sound is written down up to this long after it began
    AHEAD_S = 1.0            # the audio position is only known to within the transport's buffer
    KEEP = 40                # lines remembered per run

    def __init__(self) -> None:
        self.lines: list[tuple[float, float, str]] = []      # (starts at, ends at, text), seconds of audio in this run
        self.total = 0.0
        self.over = False                                    # the run has ended: the next line starts a new one

    def add(self, text: str, seconds: float) -> None:
        """Kairo's voice was given this line; it is `seconds` long."""
        if self.over:
            self.lines, self.total, self.over = [], 0.0, False
        self.lines.append((self.total, self.total + max(0.0, seconds), text))
        self.total += max(0.0, seconds)
        del self.lines[:-self.KEEP]

    def stopped(self) -> None:
        """The voice went quiet: what it said stays for the words still being written down, then a new run starts."""
        self.over = True

    def near(self, played_s: float | None) -> list[str]:
        """The lines the microphone can be hearing at `played_s` seconds into the run (None: the voice is quiet now)."""
        if played_s is None:
            return [t for _, _, t in self.lines[-self.LAST:]]
        near = [t for a, b, t in self.lines if b >= played_s - self.BEHIND_S and a <= played_s + self.AHEAD_S]
        return near[-(self.LAST + 1):]


def interrupts(text: str, said: Iterable[str]) -> bool:
    """The whole rule: the owner's words over Kairo's voice stop it."""
    return barge_in(text) and not echo(text, said)
