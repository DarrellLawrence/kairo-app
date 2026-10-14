"""Chat: threads, history and streaming replies from the brain.

One brain call at a time (KICKOFF 7): every call goes through BrainRunner,
which holds a lock for the whole run.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from typing import AsyncIterator

from sqlmodel import Session, select, update

from .brain import Brain, BrainEvent, EventKind
from .db import Approval, ChatMessage, ChatThread, SettingKV, utcnow
from .profile import Profile



class BrainRunner:
    def __init__(self, brain: Brain) -> None:
        self.brain = brain
        self._lock = threading.Lock()
        # The run in progress (one at a time): who started it and for which thread.
        # Tool calls arriving through mcp_kairo belong to this run.
        self.current_origin: str | None = None
        self.current_thread_id: int | None = None
        self.on_usage = None   # (purpose, usage dict, ok) -> None: More -> Usage

    def _note(self, purpose: str, event: BrainEvent) -> None:
        if self.on_usage and event.kind in (EventKind.result, EventKind.error):
            try:
                self.on_usage(purpose, (event.data or {}).get("usage"), event.kind == EventKind.result)
            except Exception:   # usage bookkeeping never breaks a run
                pass

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def cancel(self) -> None:
        cancel = getattr(self.brain, "cancel", None)
        if cancel is not None:
            cancel()

    async def stream(self, prompt: str, *, session_id: str | None, system: str, tools: list[str],
                     origin: str = "owner_chat", thread_id: int | None = None, model: str | None = None,
                     purpose: str = "chat") -> AsyncIterator[BrainEvent]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[BrainEvent | None] = asyncio.Queue()

        def post(item: BrainEvent | None) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                pass  # the caller's event loop is gone (shutdown); finish the run anyway

        def work() -> None:
            with self._lock:
                self.current_origin, self.current_thread_id = origin, thread_id
                try:
                    kwargs = {"model": model} if model else {}
                    for event in self.brain.run(prompt, session_id=session_id, system=system, tools=tools, origin=origin, **kwargs):
                        self._note(purpose, event)
                        post(event)
                except Exception as exc:  # adapter bug: report, don't kill the server
                    post(BrainEvent(EventKind.error, text=f"Brain error: {exc}"))
                finally:
                    self.current_origin, self.current_thread_id = None, None
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
    tools: jobs give the model the data and get JSON back; the backend acts."""
    with runner._lock:
        runner.current_origin, runner.current_thread_id = origin, None
        parts: list[str] = []
        final, status, error = None, "ok", ""
        try:
            kwargs = {"images": images} if images else {}
            if model:
                kwargs["model"] = model
            for ev in runner.brain.run(prompt, session_id=None, system=system, tools=[], origin=origin, **kwargs):
                runner._note(purpose, ev)
                if ev.kind == EventKind.text:
                    parts.append(ev.text)
                elif ev.kind == EventKind.result:
                    final = ev.text
                elif ev.kind == EventKind.error:
                    error = ev.text or "brain error"
                    status = (ev.status.value if ev.status else "unavailable")
        except Exception as exc:  # adapter bug: the job fails, Kairo keeps running
            error, status = f"Brain error: {exc}", "unavailable"
        finally:
            runner.current_origin = None
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
