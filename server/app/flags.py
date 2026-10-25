"""Owner-only switches for changes that wait for the owner's OK or for a measurement on the PC (overnight build
rules: every change ships behind a flag). Each flag has a default: on for plain fixes, off for anything the
owner has to decide or the PC has to prove first. The owner flips them in More -> System -> Experiments
(GET/PUT /api/flags, owner-only like every route). The model can't: settings_set doesn't know these keys.

Groups add their flags under their own marker, so parallel branches merge cleanly.
"""

from __future__ import annotations

from sqlmodel import Session

from .db import SettingKV

PREFIX = "flag:"

# name -> (default, what it changes, in the owner's words)
FLAGS: dict[str, tuple[bool, str]] = {
    # -- G1 Claude CLI

    # -- G2 Whisper

    # -- G3 mouth
    "g3.llama_flags": (True, "The voice model's server keeps no extra copies of old prompts in the PC's memory "
                             "(--cache-ram 0) and at most 8 saved checkpoints (--ctx-checkpoints 8): the memory use "
                             "grew about 33 MiB with every call. Applies the next time the voice model starts."),
    "g3.gemma_markup": (True, "Gemma's tool-call markup (<|tool_call>...<tool_call|>) is taken out of what Kairo says on a call, "
                              "like the other models' markers; without it the voice would read it out when Gemma talks."),
    "g3.constant_tools": (False, "The voice model's start of every call stays the same: its system text and tool list never change "
                                 "(a turn that must not call the tool says 'none'), and today's card and the bring-up list "
                                 "come in the first message instead, slowest-changing first. Less of the prompt is read again at "
                                 "the start of a call and when a turn switches between small talk and a question. Waits for the "
                                 "voice lab before/after on the PC (the card moves from the system role to a user message). "
                                 "Applies from the next call."),
    "g3.swa_full": (False, "Gemma's cache kept whole (--swa-full): +280 MiB of graphics-card memory, so the cached start of the "
                           "prompt survives from one turn to the next. Wait for the PC check (the card's memory) before "
                           "turning it on. Applies the next time the voice model starts."),

    "g3.gemma_first": (False, "Gemma 4 E4B is the voice model Kairo starts with, and the voice lab picks it whenever it passes "
                              "the lab's checks, even if another model answers faster. LFM2.5 2.6B was measured speaking its own "
                              "reasoning aloud (2 of 3 probe turns), which the lab does not catch. Nothing is downloaded or removed: "
                              "LFM stays installed and can still be picked by hand in Voice lab. With this on, the lab also leaves "
                              "out any model that spoke tool-call markup in its test replies (the report shows the count either way). "
                              "A model the lab or you already picked stays until the lab is run again. Gemma uses about 3.5 GiB "
                              "of graphics memory: turn it on after the PC memory check."),

    "g3.smart_turn_pin": (False, "Calls load the exact Smart Turn v3.2 file the installer downloaded and checked (pinned revision and "
                                 "checksum in voicellm.json) instead of the copy inside the Pipecat package. The same bytes today, so "
                                 "nothing sounds different; it only keeps a later package update from changing the turn detector "
                                 "unnoticed. Does nothing yet: voicellm.json carries no pin until you approve the separate update that "
                                 "adds it (that update makes the installer download llama.cpp again, about 624 MiB). Even then, if the "
                                 "downloaded file is not the pinned one, calls keep using Pipecat's copy. Applies from the next call."),

    # -- G4 jitter buffer

    # -- G5 hand-off

    # -- G6 turn-taking

    # -- G7 phone

    # -- G8 Windows

    # -- G9 speech text

    # -- G10 fast paths

    # -- G11 router

}


def on(s: Session, name: str) -> bool:
    """The flag's value: the owner's choice, else its default. An unknown name is off (default-deny)."""
    if name not in FLAGS:
        return False
    row = s.get(SettingKV, PREFIX + name)
    if row is None or row.value not in ("0", "1"):
        return FLAGS[name][0]
    return row.value == "1"


def listing(s: Session) -> list[dict]:
    return [{"name": n, "on": on(s, n), "default": d, "what": w} for n, (d, w) in sorted(FLAGS.items())]


def set_flag(s: Session, name: str, value: bool | None) -> list[dict]:
    """value None goes back to the default."""
    if name not in FLAGS:
        raise KeyError(name)
    row = s.get(SettingKV, PREFIX + name)
    if value is None:
        if row is not None:
            s.delete(row)
    else:
        row = row or SettingKV(key=PREFIX + name)
        row.value = "1" if value else "0"
        s.add(row)
    s.commit()
    return listing(s)
