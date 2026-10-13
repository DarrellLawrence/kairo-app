"""Kairo API: health, chat threads and the chat websocket. Serves the PWA too.

Run: uvicorn app.main:create_app --factory
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from sqlalchemy import inspect, text
from sqlmodel import select

from . import chat
from .brain import BrainStatus, EventKind, make_brain
from .db import ChatMessage, ChatThread, make_engine, session_for
from .profile import load_profile, render_personality
from .settings import Settings, get_settings

MAX_PROMPT_CHARS = 20_000


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    profile = load_profile(settings.config_dir)
    system_prompt = render_personality(settings.config_dir, profile)
    if settings.db_url.startswith("sqlite:///"):
        Path(settings.db_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.db_url)
    runner = chat.BrainRunner(make_brain(settings))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        engine.dispose()

    app = FastAPI(title="Kairo", lifespan=lifespan)
    app.state.settings = settings
    app.state.runner = runner

    @app.get("/health")
    def health():
        db_ok = True
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                migrated = inspect(conn).has_table("alembic_version")
        except Exception:
            db_ok, migrated = False, False
        brain = runner.brain.health()
        ok = db_ok and migrated
        body = {
            "status": "ok" if ok else "error",
            "db": "ok" if db_ok else "error",
            "migrated": migrated,
            "brain": brain.value,
            "brain_busy": runner.busy,
        }
        if not ok:
            raise HTTPException(status_code=503, detail=body)
        return body

    @app.get("/api/threads")
    def list_threads(limit: int = 30):
        with session_for(engine) as s:
            rows = s.exec(select(ChatThread).order_by(ChatThread.started_at.desc(), ChatThread.id.desc()).limit(limit)).all()
            return [{"id": t.id, "title": t.title, "started_at": t.started_at, "brain": t.brain} for t in rows]

    @app.post("/api/threads")
    def create_thread():
        with session_for(engine) as s:
            t = chat.new_thread(s, brain_name=runner.brain.name, profile=profile)
            return {"id": t.id, "title": t.title, "started_at": t.started_at, "brain": t.brain}

    @app.get("/api/threads/{thread_id}/messages")
    def thread_messages(thread_id: int):
        with session_for(engine) as s:
            if s.get(ChatThread, thread_id) is None:
                raise HTTPException(404, "thread not found")
            rows = s.exec(select(ChatMessage).where(ChatMessage.thread_id == thread_id).order_by(ChatMessage.id)).all()
            return [{"id": m.id, "role": m.role, "text": m.text, "created_at": m.created_at} for m in rows]

    @app.websocket("/ws/chat")
    async def chat_ws(ws: WebSocket):
        """Protocol (JSON):
        client -> {"type": "message", "text": "...", "thread_id": 3 | null}
        server -> {"type": "thread", "id": 3}
                  {"type": "delta", "text": "..."}            (0..n)
                  {"type": "done", "text": "full reply", "message_id": 9}
                  {"type": "error", "message": "...", "status": "usage_limited" | ...}
        """
        await ws.accept()
        try:
            while True:
                data = await ws.receive_json()
                if not isinstance(data, dict) or data.get("type") != "message":
                    await ws.send_json({"type": "error", "message": "unknown message type"})
                    continue
                text_in = str(data.get("text") or "").strip()
                if not text_in:
                    await ws.send_json({"type": "error", "message": "empty message"})
                    continue
                if len(text_in) > MAX_PROMPT_CHARS:
                    await ws.send_json({"type": "error", "message": f"message longer than {MAX_PROMPT_CHARS} characters"})
                    continue
                await _handle_message(ws, data.get("thread_id"), text_in)
        except WebSocketDisconnect:
            return

    async def _handle_message(ws: WebSocket, thread_id, text_in: str) -> None:
        with session_for(engine) as s:
            thread = s.get(ChatThread, thread_id) if isinstance(thread_id, int) else None
            if thread is None:
                thread = chat.today_thread(s, brain_name=runner.brain.name, profile=profile)
            chat.add_message(s, thread.id, "owner", text_in)
            session_id = thread.brain_session_id
            tid = thread.id
        await ws.send_json({"type": "thread", "id": tid})

        parts: list[str] = []
        final_text: str | None = None
        error = None
        new_sid = None
        async for event in runner.stream(text_in, session_id=session_id, system=system_prompt, tools=chat.CHAT_TOOLS):
            if event.kind is EventKind.session and event.session_id:
                new_sid = event.session_id
            elif event.kind is EventKind.text:
                parts.append(event.text)
                await ws.send_json({"type": "delta", "text": event.text})
            elif event.kind is EventKind.result:
                final_text = event.text or "".join(parts)
                new_sid = event.session_id or new_sid
            elif event.kind is EventKind.error:
                error = event
                new_sid = event.session_id or new_sid

        with session_for(engine) as s:
            thread = s.get(ChatThread, tid)
            if new_sid and thread.brain_session_id != new_sid:
                thread.brain_session_id = new_sid
                s.add(thread)
                s.commit()
            if error is not None:
                status = (error.status or BrainStatus.unavailable).value
                chat.add_message(s, tid, "system", f"[{status}] {error.text}")
                await ws.send_json({"type": "error", "message": error.text, "status": status})
                return
            msg = chat.add_message(s, tid, "kairo", final_text or "")
        await ws.send_json({"type": "done", "text": final_text or "", "message_id": msg.id})

    _mount_web(app, settings.web_dist_dir)
    return app


def _mount_web(app: FastAPI, dist: Path) -> None:
    """Serve the built PWA if present. Unknown paths fall back to index.html."""
    index = dist / "index.html"
    if not index.is_file():
        return
    dist_resolved = dist.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith(("api/", "ws/")):
            raise HTTPException(404)
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(dist_resolved):
            return FileResponse(candidate)
        return FileResponse(index)

