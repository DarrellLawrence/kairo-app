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


# ---------------------------------------------------------------- which label the turn being handled has

import contextvars
import json
import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

log = logging.getLogger("kairo.speaker_label")

KEY = "voice.turn"                      # the settings row the gate and the hook read (gate_context.VOICE_TURN_KEY)
FLAG = "g13.speaker_label_gate"

_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("kairo_voice_label", default=None)


def current() -> str | None:
    """The label of the voice turn the running code is handling, or None (not a voice turn)."""
    return _current.get()


@contextmanager
def scope(label: str | None) -> Iterator[None]:
    """Everything inside, tasks and worker threads it starts included (asyncio copies the context), is part of a voice
    turn with this label. A label nobody knows is refused here, not discovered later by the gate."""
    if label is not None and not (isinstance(label, str) and label in LABELS):
        raise ValueError(f"unknown speaker label {label!r}")
    token = _current.set(label)
    try:
        yield
    finally:
        _current.reset(token)


# ---------------------------------------------------------------- the row the hook reads

class VoiceTurns:
    """The label of the voice turn Claude is running, in the database, for the PreToolUse hook and the gate. Kept by
    chat.BrainRunner: begin() right after the run holds the brain and before Claude starts, end() when it is over.
    One run at a time, so one row. Nothing the model can call writes here."""

    def __init__(self, engine):
        self.engine = engine

    def begin(self, label: str | None, *, thread_id: int | None = None, purpose: str | None = None) -> str | None:
        """Write the label (None: a turn that is not a voice turn: only remove what a crash left). Returns the run's id
        for end(), or None when nothing was written (no label, or g13.speaker_label_gate is off). Raises when the label is
        not one Kairo knows or the database fails: the caller must then not run Claude. thread_id and purpose say which
        conversation and which kind of run it is (kept with the label; never words)."""
        from sqlmodel import Session

        from . import flags
        from .db import SettingKV
        if label is not None and not (isinstance(label, str) and label in LABELS):
            raise ValueError(f"unknown speaker label {label!r}")
        if label is None:                                        # not a voice turn: tidy up, and never stop a typed message over it
            try:
                self._clear()
            except Exception:
                log.warning("speaker label: a leftover record could not be removed", exc_info=True)
            return None
        with Session(self.engine) as s:
            if not flags.on(s, FLAG):
                self._clear(s)                                   # (the flag was just turned off: no leftover)
                return None
            run = uuid.uuid4().hex[:12]
            row = s.get(SettingKV, KEY) or SettingKV(key=KEY)
            row.value = json.dumps({"label": label, "run": run, "thread": thread_id, "purpose": purpose,
                                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            s.add(row)
            s.commit()
            return run

    def _clear(self, s=None) -> None:
        from sqlmodel import Session

        from .db import SettingKV
        if s is None:
            with Session(self.engine) as own:
                return self._clear(own)
        row = s.get(SettingKV, KEY)
        if row is not None:
            s.delete(row)
            s.commit()

    def end(self, run: str | None) -> None:
        """Remove the record this run wrote (and only that one)."""
        if run is None:
            return
        from sqlmodel import Session

        from .db import SettingKV
        with Session(self.engine) as s:
            row = s.get(SettingKV, KEY)
            if row is None:
                return
            try:
                mine = json.loads(row.value or "")["run"] == run
            except (TypeError, ValueError, KeyError):
                mine = False                                     # not readable: the next begin() replaces it
            if mine:
                s.delete(row)
                s.commit()
