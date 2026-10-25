"""Chat: threads, history and streaming replies from the brain.

One brain call at a time (KICKOFF 7): every call goes through BrainRunner,
which holds a lock for the whole run. The owner goes first (fix plan 28): chat,
calls and voice notes get the brain before any waiting job, and a job's call
that is running when the owner talks is stopped and redone afterwards (jobs have
no tools, so a stopped job call changed nothing). A job holds the brain for one
call at a time, never across calls.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, AsyncIterator, Iterator

from sqlmodel import Session, select, update

from . import speaker_label
from .brain import Brain, BrainEvent, EventKind
from .db import Approval, ChatMessage, ChatThread, SettingKV, utcnow
from .profile import Profile

log = logging.getLogger("kairo.chat")

# A job call is stopped for the owner at most this often; after that it gives up until its next run
# (Voice V1: the owner never waits for a job).
MAX_JOB_PREEMPTS = 2
OWNER_QUIET_S = 60        # after an owner turn, jobs wait this long: the owner may go on talking
MAX_QUIET_WAIT_S = 1800   # ... but never longer than this in a row (a call left open all day)


class BrainRunner:
    def __init__(self, brain: Brain, quiet_s: float = 0.0) -> None:
        self.brain = brain
        self.quiet_s = quiet_s                # main.py: OWNER_QUIET_S; tests: none
        self._owner_done_at = float("-inf")
        self._lines = 0                       # voice lines open (a call, a spoken message)
        self._cond = threading.Condition()
        self._held_by: str | None = None      # "owner" | "job"
        self._owners_waiting = 0
        self._job_preemptible = False
        self._job_preempted = False
        # The run in progress (one at a time): who started it and for which thread.
        # Tool calls arriving through mcp_kairo belong to this run.
        self.current_origin: str | None = None
        self.current_thread_id: int | None = None
        self.on_usage = None   # (purpose, usage dict, ok, reason=None) -> None: More -> Usage
        self.router = None     # app/router.Router (G11): logs the route of every owner turn; None: no routing
        self.voice_turns = None   # app/speaker_label.VoiceTurns (G13): the speaker label of a voice turn, for the gate; None: none
        self.last_owner_wait_ms = 0   # how long the last owner turn waited for the brain (app/turns.py)

    def _note(self, purpose: str, event: BrainEvent, reason: str | None = None) -> None:
        """reason: why this run isn't on Haiku (conversation.route_why), shown under More -> Usage."""
        if self.on_usage and event.kind in (EventKind.result, EventKind.error):
            try:
                self.on_usage(purpose, (event.data or {}).get("usage"), event.kind == EventKind.result, reason=reason)
            except Exception:   # usage bookkeeping never breaks a run
                pass

    @property
    def busy(self) -> bool:
        return self._held_by is not None

    def lines_open(self) -> int:
        """Voice lines open right now (a call, a spoken message): the router keeps the local model for them."""
        return self._lines

    def line_open(self) -> None:
        with self._cond:
            self._lines += 1

    def line_closed(self) -> None:
        with self._cond:
            self._lines = max(0, self._lines - 1)
            self._cond.notify_all()

    def _owner_near(self) -> bool:
        """The owner is in a conversation: on a call, or talked a moment ago."""
        return self._lines > 0 or time.monotonic() - self._owner_done_at < self.quiet_s

    @contextmanager
    def hold(self, *, owner: bool, preemptible: bool = False) -> Iterator[None]:
        """The brain, one call at a time. owner=True: before every waiting job, and a running job
        call that is preemptible gets stopped (re-checked every half second, so a call that was
        just starting is caught too). A job also waits while the owner is in a conversation (a voice
        line is open, or they talked less than quiet_s ago). Not re-entrant."""
        with self._cond:
            if owner:
                self._owners_waiting += 1
            asked = time.monotonic()
            try:
                while self._held_by is not None or (not owner and (
                        self._owners_waiting or (self._owner_near() and time.monotonic() - asked < MAX_QUIET_WAIT_S))):
                    if owner and self._held_by == "job" and self._job_preemptible:
                        self._job_preempted = True
                        self._stop_brain()
                    self._cond.wait(0.5)
                self._held_by = "owner" if owner else "job"
                self._job_preemptible = preemptible and not owner
                self._job_preempted = False
            finally:
                if owner:
                    self._owners_waiting -= 1
        try:
            yield
        finally:
            with self._cond:
                if self._held_by == "owner":
                    self._owner_done_at = time.monotonic()
                self._held_by = None
                self._job_preemptible = False
                self._cond.notify_all()

    def _stop_brain(self) -> None:
        cancel = getattr(self.brain, "cancel", None)
        if cancel is not None:
            cancel()

    def cancel(self) -> None:
        """Kill switch: stop the running call and any warm process."""
        self._stop_brain()
        close_warm = getattr(self.brain, "close_warm", None)
        if close_warm is not None:
            close_warm()

    async def stream(self, prompt: str, *, session_id: str | None, system: str, tools: list[str],
                     origin: str = "owner_chat", thread_id: int | None = None, model: str | None = None,
                     purpose: str = "chat", warm: Any = None, reason: str | None = None) -> AsyncIterator[BrainEvent]:
        """An owner turn (chat, call, voice note, self-test): first in line for the brain.
        warm: a warm process key (calls, fix plan 30), see ClaudeCLIBrain.warm_up. reason: why the model
        isn't Haiku, recorded with the run."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[BrainEvent | None] = asyncio.Queue()
        voice = speaker_label.current()   # G13: the caller's context, read here (the thread below does not inherit it)

        def post(item: BrainEvent | None) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                pass  # the caller's event loop is gone (shutdown); finish the run anyway

        def work() -> None:
            if self.router is not None:   # G11: the owner's turns have tools, so the subscription is the only route; logged
                d = self.router.route(purpose, prompt, mode="call" if purpose in ("call", "learn_call") else "owner",
                                      model=model, tools=bool(tools))
                if d.route == "none":
                    post(BrainEvent(EventKind.error, text=f"No model route is allowed ({d.reason}). See config/routing.yaml."))
                    post(None)
                    return
            asked = time.monotonic()
            with self.hold(owner=True):
                self.last_owner_wait_ms = int((time.monotonic() - asked) * 1000)
                self.current_origin, self.current_thread_id = origin, thread_id
                labelled = None
                try:
                    if self.voice_turns is not None:
                        # G13: who spoke, in the database, once this run holds the brain and before Claude starts (so a
                        # waiting turn never takes the label of the one running). Not written: Claude does not run.
                        labelled = self.voice_turns.begin(voice)
                    kwargs = {"model": model} if model else {}
                    if warm is not None:
                        kwargs["warm"] = warm
                    for event in self.brain.run(prompt, session_id=session_id, system=system, tools=tools, origin=origin, **kwargs):
                        self._note(purpose, event, reason)
                        post(event)
                except Exception as exc:  # adapter bug: report, don't kill the server
                    post(BrainEvent(EventKind.error, text=f"Brain error: {exc}"))
                finally:
                    self.current_origin, self.current_thread_id = None, None
                    try:
                        self.voice_turns.end(labelled) if self.voice_turns is not None else None
                    except Exception:   # the next run replaces a leftover; it only matters for the owner_chat tools after this
                        log.exception("speaker label: the record of the turn could not be removed")
                    post(None)

        threading.Thread(target=work, name="brain-run", daemon=True).start()
        while (event := await queue.get()) is not None:
            yield event


class AskResult:
    """What a background job got back from one brain run."""

    def __init__(self, text: str, status: str = "ok", error: str = "") -> None:
        self.text, self.status, self.error = text, status, error

    @property
    def ok(self) -> bool:
        return self.status == "ok" and not self.error


def ask_sync(runner: BrainRunner, prompt: str, *, system: str, origin: str = "job",
             images: list[tuple[bytes, str]] | None = None, model: str | None = None, purpose: str = "job") -> AskResult:
    """One brain run for a job, blocking (jobs run in scheduler threads). No
    tools: jobs give the model the data and get JSON back; the backend acts.
    Waits while the owner talks; stopped for the owner, it runs again after."""
    for _attempt in range(MAX_JOB_PREEMPTS + 1):
        with runner.hold(owner=False, preemptible=True):
            result = _ask_once(runner, prompt, system=system, origin=origin, images=images, model=model, purpose=purpose)
            preempted = runner._job_preempted
        if not (preempted and result is None):
            break
    return result or AskResult("", "unavailable", "Stopped for the owner too often.")


def _ask_once(runner: BrainRunner, prompt: str, *, system, origin, images, model, purpose) -> AskResult | None:
    """None: stopped before it finished (the owner went first)."""
    runner.current_origin, runner.current_thread_id = origin, None
    parts: list[str] = []
    final, status, error, cancelled = None, "ok", "", False
    try:
        kwargs = {"images": images} if images else {}
        if model:
            kwargs["model"] = model
        for ev in runner.brain.run(prompt, session_id=None, system=system, tools=[], origin=origin, **kwargs):
            if ev.kind == EventKind.text:
                parts.append(ev.text)
            elif ev.kind == EventKind.result:
                final = ev.text
            elif ev.kind == EventKind.error:
                error = ev.text or "brain error"
                status = (ev.status.value if ev.status else "unavailable")
                cancelled = bool(ev.data.get("cancelled"))
            if not (cancelled and runner._job_preempted):
                runner._note(purpose, ev)
    except Exception as exc:  # adapter bug: the job fails, Kairo keeps running
        error, status = f"Brain error: {exc}", "unavailable"
    finally:
        runner.current_origin = None
    if final is None and cancelled and runner._job_preempted:
        return None
    return AskResult(final if final is not None else "".join(parts), status, error)


def local_day(ts: datetime, profile: Profile):
    return ts.astimezone(profile.tz).date()


MAIN_KEY = "chat.main_thread"


def main_thread(session: Session, *, brain_name: str) -> ChatThread:
    """The one conversation with Kairo (owner's request 2026-10-14): chat, calls and voice notes.
    There is no other thread and no "new chat"; app/memory.py keeps what's older."""
    row = session.get(SettingKV, MAIN_KEY)
    thread = session.get(ChatThread, int(row.value)) if row and row.value and row.value.isdigit() else None
    if thread is None:
        thread = session.exec(select(ChatThread).order_by(ChatThread.id)).first()
        if thread is None:
            thread = ChatThread(brain=brain_name, started_at=utcnow(), title="Kairo")
            session.add(thread)
            session.commit()
            session.refresh(thread)
        row = row or SettingKV(key=MAIN_KEY)
        row.value = str(thread.id)
        session.add(row)
        session.commit()
    return thread


def consolidate(session: Session, *, brain_name: str) -> int:
    """The day threads from before become the one conversation (messages and card links keep
    their order: ids grow with time). Returns how many threads were merged."""
    main = main_thread(session, brain_name=brain_name)
    others = [t.id for t in session.exec(select(ChatThread).where(ChatThread.id != main.id)).all()]
    if not others:
        return 0
    session.exec(update(ChatMessage).where(ChatMessage.thread_id.in_(others)).values(thread_id=main.id))
    session.exec(update(Approval).where(Approval.thread_id.in_(others)).values(thread_id=main.id))
    for tid in others:
        session.delete(session.get(ChatThread, tid))
    main.brain_session_id = None          # the next turn starts fresh, with the memory and recent messages
    main.title = "Kairo"
    session.add(main)
    session.commit()
    return len(others)


def add_message(session: Session, thread_id: int, role: str, text: str) -> ChatMessage:
    msg = ChatMessage(thread_id=thread_id, role=role, text=text)
    session.add(msg)
    session.commit()
    session.refresh(msg)
    return msg
