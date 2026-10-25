"""The speaker label of a voice turn (G13, owner's prompt 0.1: "block actions for non-matching voices; speaker label
passed to the PreToolUse hook").

owner      the speaker check heard the owner's voice (score at or over the threshold)
unsure     the check is on but could not say: a short turn with a weak score, too little audio, a check that was
           too slow or crashed
stranger   the check heard someone else (such a turn is not answered at all; the label is kept for the record)
unchecked  the check is off or Kairo has no voiceprint yet (and the voice button, which is the owner's own press):
           today's behaviour, and `kairo diag` warns

The label comes from the backend, never from anything the model sends. Unless it is owner or unchecked, nothing that
changes anything runs on that turn (gate.py).
"""

from __future__ import annotations

OWNER, UNSURE, STRANGER, UNCHECKED = "owner", "unsure", "stranger", "unchecked"
LABELS = (OWNER, UNSURE, STRANGER, UNCHECKED)       # gate.VOICE_LABELS says the same; a test keeps them equal

# Least trusted first. When several verdicts could belong to one turn, the least trusted one counts: no order of
# events can make a turn more trusted than its own verdict. (owner and unchecked both let a turn change things.)
_TRUST = {STRANGER: 0, UNSURE: 1, UNCHECKED: 2, OWNER: 3}


def worst(*labels: str | None) -> str | None:
    """The least trusted of the labels (None ones left out); None when there are none. An unknown label is the worst."""
    known = [x for x in labels if x is not None]
    return min(known, key=lambda x: _TRUST.get(x, -1)) if known else None
