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

    # -- G9 speech text

    # -- G10 fast paths

    # -- G11 router
    "g11.router": (False, "Kairo writes down, for every call to the model, which route it took and why (More -> Usage, "
                          "\"Routes\"), after checking the privacy class of the content. Today the route is always your "
                          "Claude subscription, so nothing else changes. Off: no log and no check, as before."),
    "g11.local_route": (False, "The check whether a mail needs a reply (only that job) may run on the voice model on this PC "
                               "instead of Claude, but only after the router lab has passed it (not yet: the lab needs the "
                               "PC), only when the voice model's server has a second slot for jobs, and never while a call "
                               "is open; an answer that is not exactly right goes to Claude. Needs g11.router too. Off: "
                               "everything runs on Claude."),

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
