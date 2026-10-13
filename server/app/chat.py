"""Chat: threads, history and streaming replies from the brain.

One brain call at a time (KICKOFF 7): every call goes through BrainRunner,
which holds a lock for the whole run.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from typing import AsyncIterator

from sqlmodel import Session, select

from .brain import Brain, BrainEvent, EventKind
from .db import ChatMessage, ChatThread, utcnow
from .profile import Profile

# Milestone A: no tools yet. Milestone B adds read tools, internal Kairo tools
# and mcp__kairo__propose_action (KICKOFF 7). Never Google write tools.
CHAT_TOOLS: list[str] = []


class BrainRunner:
    def __init__(self, brain: Brain) -> None:
        self.brain = brain
        self._lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def stream(self, prompt: str, *, session_id: str | None, system: str, tools: list[str]) -> AsyncIterator[BrainEvent]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[BrainEvent | None] = asyncio.Queue()

        def post(item: BrainEvent | None) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                pass  # the caller's event loop is gone (shutdown); finish the run anyway

        def work() -> None:
            with self._lock:
                try:
                    for event in self.brain.run(prompt, session_id=session_id, system=system, tools=tools):
                        post(event)
                except Exception as exc:  # adapter bug: report, don't kill the server
                    post(BrainEvent(EventKind.error, text=f"Brain error: {exc}"))
                finally:
                    post(None)

        threading.Thread(target=work, name="brain-run", daemon=True).start()
        while (event := await queue.get()) is not None:
            yield event


def local_day(ts: datetime, profile: Profile):
    return ts.astimezone(profile.tz).date()


def today_thread(session: Session, *, brain_name: str, profile: Profile) -> ChatThread:
    """New thread per day by default (KICKOFF 7)."""
    latest = session.exec(select(ChatThread).order_by(ChatThread.started_at.desc(), ChatThread.id.desc())).first()
    now = utcnow()
    if latest is not None:
        started = latest.started_at if latest.started_at.tzinfo else latest.started_at.replace(tzinfo=now.tzinfo)
        if local_day(started, profile) == local_day(now, profile):
            return latest
    return new_thread(session, brain_name=brain_name, profile=profile)


def new_thread(session: Session, *, brain_name: str, profile: Profile) -> ChatThread:
    now = utcnow()
    thread = ChatThread(brain=brain_name, started_at=now, title=local_day(now, profile).isoformat())
    session.add(thread)
    session.commit()
    session.refresh(thread)
    return thread


def add_message(session: Session, thread_id: int, role: str, text: str) -> ChatMessage:
    msg = ChatMessage(thread_id=thread_id, role=role, text=text)
    session.add(msg)
    session.commit()
    session.refresh(msg)
    return msg
