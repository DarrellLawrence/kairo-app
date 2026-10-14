"""Voice notes (KICKOFF v7 item 7): say it, hang up, Kairo handles it.

In:   POST /api/voice-note (iPhone Shortcut, capture token, raw audio body) or
      POST /api/voice-notes (the app, e.g. hanging up a call mid-sentence).
      Answers at once; the work runs in the background.
Then: transcribe on the PC -> run it like a chat message in the one conversation
      (origin "voice_note", tools as in chat) -> Kairo's short answer is saved
      there and on the capture, shown next time the app opens. Push only when
      the model marks it URGENT.
Own-only things (tasks, notes, labels, own calendar) just happen. The gate
denies approval cards for this origin: anything that would reach another
person is prepared and waits for the owner in the app.
Fallbacks: no speech-to-text -> the capture job transcribes it later; no
model -> the capture job sorts the text into a task or note.
"""

from __future__ import annotations

import asyncio
import json
import logging

from sqlmodel import Session

from . import capture, chat, memory
from .brain import EventKind
from .db import Capture, ChatThread, utcnow
from .jobkit import clip

log = logging.getLogger("kairo.voicenote")

URGENT = "URGENT:"
RULES = """
This message is a voice note: the owner said it and hung up, nobody is waiting.
Do what it asks with your tools. Things that only touch the owner (tasks, notes,
reminders, labels, own calendar): just do them. Anything that would reach
another person can't be sent from a voice note: prepare it (a draft) and say it
waits in the app. Answer in ONE short line: what you did and where it went (and
what waits for the owner, if anything). Start with "URGENT:" only if the owner
must act within the next hour."""


class VoiceNotes:
    def __init__(self, *, engine, inbox_dir, profile, runner, state, system, tools, pick_model, paused, recall=None):
        self.engine, self.inbox_dir, self.profile = engine, inbox_dir, profile
        self.runner, self.state = runner, state   # app.state: voice and pusher are looked up when used
        self.system, self.tools, self.pick_model, self.paused = system, tools, pick_model, paused
        self.recall = recall                      # v8 C6: what the owner's notes say about this note
        self._tasks: set[asyncio.Task] = set()

    def accept(self, data: bytes, content_type: str, source: str) -> Capture:
        """Store the recording (fast). Status 'voice' keeps the capture job's hands off it."""
        with Session(self.engine) as s:
            cap = capture.store_file(s, self.inbox_dir, self.profile, data=data, content_type=content_type,
                                     note=None, source=source)
            if cap.kind != "audio":
                raise capture.CaptureError("send a voice recording (audio)")
            cap.status = "voice"
            s.add(cap)
            s.commit()
            s.refresh(cap)
            return cap

    def start(self, cap_id: int) -> None:
        task = asyncio.get_running_loop().create_task(self.process(cap_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait(self) -> None:
        """Tests: until every background note is handled."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _set(self, cap_id: int, **fields) -> None:
        with Session(self.engine) as s:
            cap = s.get(Capture, cap_id)
            for k, v in fields.items():
                setattr(cap, k, v)
            s.add(cap)
            s.commit()

    async def process(self, cap_id: int) -> None:
        try:
            await self._process(cap_id)
        except Exception:   # never lose a note: the capture job picks it up
            log.exception("voice note %s failed", cap_id)
            with Session(self.engine) as s:
                cap = s.get(Capture, cap_id)
                if cap is not None and cap.status == "voice":
                    cap.status = "new" if cap.text else "later"
                    s.add(cap)
                    s.commit()

    async def _process(self, cap_id: int) -> None:
        with Session(self.engine) as s:
            path = s.get(Capture, cap_id).raw_path
        with open(path, "rb") as fh:
            audio = fh.read()
        try:
            heard = await asyncio.to_thread(self.state.voice.transcribe, audio, None)
        except Exception as exc:   # not installed / unreadable: the capture job retries
            log.info("voice note %s: no transcript yet (%s)", cap_id, exc)
            self._set(cap_id, status="later")
            return
        text = (heard.text or "").strip()
        if not text:
            self._set(cap_id, text="(empty voice note)", status="filed", processed=True, filed_as="voice_note",
                      title="Empty voice note", filed_at=utcnow())
            return
        self._set(cap_id, text=text, title=clip(text, 80))
        if self.paused():
            self._set(cap_id, status="new")   # kill switch: the capture job files it after resume
            return

        with Session(self.engine) as s:
            thread = chat.main_thread(s, brain_name=self.runner.brain.name)   # the one conversation
            msg_in = chat.add_message(s, thread.id, "owner", f"(voice note) {text}")
            if memory.take_fresh(s):
                thread.brain_session_id = None
                s.add(thread)
                s.commit()
            tid, session_id = thread.id, thread.brain_session_id
        system = self.system(thread_id=tid, fresh=session_id is None, before_id=msg_in.id)
        if self.recall is not None:
            system += await asyncio.to_thread(self.recall, text)
        parts, final, error, new_sid = [], None, None, None
        async for ev in self.runner.stream(f"(voice note) {text}", session_id=session_id, system=system + RULES,
                                           tools=self.tools, origin="voice_note", thread_id=tid, purpose="voice_note",
                                           model=self.pick_model("voice_note")):
            if ev.kind is EventKind.session and ev.session_id:
                new_sid = ev.session_id
            elif ev.kind is EventKind.text:
                parts.append(ev.text)
            elif ev.kind is EventKind.result:
                final, new_sid = ev.text or "".join(parts), ev.session_id or new_sid
            elif ev.kind is EventKind.error:
                error, new_sid = ev, ev.session_id or new_sid

        reply = (final or "").strip()
        urgent = reply.upper().startswith(URGENT)
        if urgent:
            reply = reply[len(URGENT):].strip()
        with Session(self.engine) as s:
            thread = s.get(ChatThread, tid)
            if new_sid and thread.brain_session_id != new_sid:
                thread.brain_session_id = new_sid
                s.add(thread)
                s.commit()
            cap = s.get(Capture, cap_id)
            if error is not None:
                chat.add_message(s, tid, "system", f"[voice note not handled] {error.text}")
                cap.status = "new"   # the capture job sorts it into a task or note
            else:
                chat.add_message(s, tid, "kairo", reply)
                cap.status, cap.processed, cap.filed_as, cap.filed_at = "filed", True, "voice_note", utcnow()
                cap.result_json = json.dumps({"type": "voice_note", "thread_id": tid, "reply": reply, "urgent": urgent})
            s.add(cap)
            s.commit()
        if urgent and error is None:
            self.state.pusher.notify("Kairo", clip(reply, 160), "/#chat")
