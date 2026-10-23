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

    # -- G4 jitter buffer

    # -- G5 hand-off

    # -- G6 turn-taking

    # -- G7 phone

    # -- G8 Windows
    "g8.embed_batch4": (True, "The memory search reads your notes 4 pieces at a time instead of 16. While it works it keeps about half "
                              "the main memory (1.4 GB instead of 2.9 GB on the processor, measured on a test machine) and gives the "
                              "same answers and about the same indexing speed. Takes effect the next time the memory model starts."),
    "g8.stagger_boot": (True, "When Kairo starts, the voice model, then Whisper and Kokoro, then the memory model load one after the other "
                              "instead of all at once, and the free graphics memory is read in between and logged. Before, the memory "
                              "model checked the card before the voice model had taken its share and could pick the GPU when it did not "
                              "fit. Takes effect the next time Kairo starts."),
    "g8.sysmem_readback": (False, "Kairo's voice report also shows NVIDIA's driver setting 'CUDA sysmem fallback' as Kairo's own account sees it "
                                  "(1 = a full graphics card is an error, anything else = the driver may quietly spill into slow shared "
                                  "memory). Off until the read has been proven on the PC: `kairo diag` reads it itself either way."),

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
