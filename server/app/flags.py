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
    "g2.temperature_zero": (True, "On calls, Whisper writes each sentence down once and never retries with random variation "
                                  "when it is unsure: the same words every time, and no slow second pass."),
    "g2.offline_models": (True, "Whisper only loads the model files the installer already downloaded and never goes online to look "
                                "for them while Kairo runs. A missing model is reported, not fetched in the middle of a sentence."),
    "g2.int8_float16": (False, "Whisper runs in a lighter number format on the graphics card (int8_float16): about 0.8 GB less "
                               "graphics memory, speed the same. Wait for the PC test (speed and word errors on your voice) "
                               "before you switch it on. Takes effect at the next call."),
    "g2.warm_at_connect": (True, "When a call or the voice button connects, Whisper writes down two short test sentences "
                                 "(English, German) in the background, so your first real sentence does not meet a cold "
                                 "graphics card."),

    # -- G3 mouth

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
