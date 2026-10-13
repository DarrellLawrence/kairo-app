"""Kairo API: health, chat threads and the chat websocket. Serves the PWA too.

Run: uvicorn app.main:create_app --factory
"""

from __future__ import annotations

import hmac
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import inspect, text
from sqlmodel import select

from . import approvals, chat, killswitch
from .audit import audit
from .executor import Executor
from .gate import TOOL_PREFIX, load_gate
from .gate_context import read_context
from .google.client import GoogleAPI, GoogleError, GoogleNotConnected
from .google.oauth import GoogleOAuth, OAuthError
from .mcp_tools import tool_list, tool_names
from .push import Pusher
from .tools import ToolRunner
from .workspace import write_workspace
from .brain import BrainStatus, EventKind, make_brain
from .db import Approval, AuditLog, ChatMessage, ChatThread, make_engine, session_for
from .profile import load_profile, render_personality
from .settings import Settings, get_settings

MAX_PROMPT_CHARS = 20_000


def same_origin(origin: str | None, *hosts: str | None) -> bool:
    """Browsers send Origin on websocket and cross-site requests; CORS does not
    protect websockets. Only the app's own origin may talk to the API, so a
    random web page open on the phone can't drive Kairo. No Origin header =
    not a browser (curl, health checks): allowed.
    """
    if origin is None:
        return True
    netloc = origin.split("://", 1)[-1].rstrip("/")
    return any(h and netloc == h for h in hosts)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    profile = load_profile(settings.config_dir)
    system_prompt = render_personality(settings.config_dir, profile)
    if settings.db_url.startswith("sqlite:///"):
        Path(settings.db_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.db_url)
    gate = load_gate(settings.config_dir)   # refuses to start on a broken or weakened permissions.yaml
    tool_token = secrets.token_urlsafe(32)  # new every start; only mcp_kairo gets it
    mcp_config = write_workspace(settings.workspace_dir,
                                 tool_url=f"http://127.0.0.1:{settings.port}/internal/tools", token=tool_token)
    hook_env = {"KAIRO_HOOK_CONFIG_DIR": str(settings.config_dir), "KAIRO_HOOK_DB_URL": settings.db_url}
    runner = chat.BrainRunner(make_brain(settings, mcp_config=mcp_config, hook_env=hook_env))
    model_tools = tool_names(gate)

    def google_read() -> GoogleAPI:
        return GoogleAPI(settings.secrets_dir, "read", transport=getattr(app.state, "google_transport", None))

    tools = ToolRunner(engine=engine, vault_dir=settings.vault_dir, google_read=google_read)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        engine.dispose()

    app = FastAPI(title="Kairo", lifespan=lifespan)
    app.state.settings = settings
    app.state.runner = runner
    app.state.gate = gate
    app.state.tool_token = tool_token
    app.state.executor = Executor(secrets_dir=settings.secrets_dir)
    app.state.pusher = Pusher(engine=engine, secrets_dir=settings.secrets_dir, profile=profile)
    app.state.on_new_card = lambda card: app.state.pusher.notify(
        f"#{card.id} {'Delete' if card.tier >= 4 else 'Approve'}?", card.summary, "/#approvals")
    app.state.oauth = GoogleOAuth(settings.secrets_dir, expected_email=profile.gmail,
                                  redirect_uri=f"http://127.0.0.1:{settings.port}/setup/google/callback")

    @app.middleware("http")
    async def reject_cross_origin(request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS") and not same_origin(
            request.headers.get("origin"), request.headers.get("host"), request.headers.get("x-forwarded-host")
        ):
            return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)
        return await call_next(request)

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

    @app.get("/api/kill-switch")
    def get_kill_switch():
        with session_for(engine) as s:
            return {"paused": killswitch.is_paused(s)}

    @app.post("/api/kill-switch")
    def set_kill_switch(body: dict):
        paused = bool(body.get("paused"))
        with session_for(engine) as s:
            killswitch.set_paused(s, paused)
            audit(s, actor="owner", action="kill_switch.on" if paused else "kill_switch.off", outcome="set")
        if paused:
            runner.cancel()
        return {"paused": paused}

    @app.get("/api/activity")
    def activity(limit: int = 200):
        with session_for(engine) as s:
            rows = s.exec(select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 1000))).all()
            return [{"id": r.id, "ts": r.ts, "actor": r.actor, "action": r.action, "tier": r.tier, "outcome": r.outcome} for r in rows]

    # ---------------------------------------------------------------- tools for the model (mcp_kairo only)

    def _check_tool_caller(request: Request) -> None:
        sent = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        if not is_loopback(request) or not hmac.compare_digest(sent, tool_token):
            raise HTTPException(403, "not mcp_kairo")

    @app.get("/internal/tools/list")
    def internal_tool_list(request: Request):
        _check_tool_caller(request)
        return {"tools": tool_list(gate)}

    @app.post("/internal/tools/call")
    def internal_tool_call(request: Request, body: dict):
        """Every model tool call lands here: gate first, then do it. Runs in a worker thread."""
        _check_tool_caller(request)
        name = str(body.get("name") or "")
        arguments = body.get("arguments") if isinstance(body.get("arguments"), dict) else {}
        origin = runner.current_origin
        with session_for(engine) as s:
            if origin is None:
                audit(s, actor="brain", action=name or "?", outcome="denied: no brain run in progress", args=arguments)
                return {"text": "Not allowed: no Kairo run in progress.", "is_error": True}
            decision = gate.decide(TOOL_PREFIX + name, arguments, read_context(engine, origin))
            if not decision.allowed:
                audit(s, actor="brain", action=decision.action or name or "?", tier=decision.tier, args=arguments,
                      outcome=f"denied: {decision.reason}")
                return {"text": f"Not allowed: {decision.reason}", "is_error": True}
            if decision.verdict == "allow":
                text_out = tools.run(decision)
                failed = text_out.startswith("Error")
                audit(s, actor="brain", action=decision.action, tier=decision.tier, args=decision.args,
                      outcome="error" if failed else "ok")
                return {"text": text_out, "is_error": failed}
            if decision.verdict == "execute":
                try:
                    result = app.state.executor.run(decision.action, decision.args)
                except (GoogleNotConnected, GoogleError, ValueError) as exc:
                    audit(s, actor="backend", action=decision.action, tier=2, args=decision.args, outcome=f"failed: {exc}")
                    return {"text": f"Error: {exc}", "is_error": True}
                audit(s, actor="backend", action=decision.action, tier=2, args=decision.args, outcome="executed (tier 2, in digest)")
                return {"text": "Done: " + json.dumps(result, default=str), "is_error": False}
            # approve: a card for the owner
            reason = str((arguments or {}).get("reason") or "")
            try:
                card = approvals.create_card(s, decision, executor=app.state.executor, google_read=google_read,
                                             thread_id=runner.current_thread_id, reason=reason)
            except (approvals.ApprovalError, GoogleNotConnected, GoogleError) as exc:
                audit(s, actor="backend", action=decision.action, tier=decision.tier, args=decision.args, outcome=f"card failed: {exc}")
                return {"text": f"Error: {exc}", "is_error": True}
            notify = getattr(app.state, "on_new_card", None)
            if notify:
                notify(card)
            return {"text": f"Approval card #{card.id} created: {card.summary}. Nothing happens until the owner "
                            f"approves it in the Kairo app.", "is_error": False}

    # ---------------------------------------------------------------- Google sign-in (only on the PC itself)

    def _page(title: str, text: str, ok: bool) -> HTMLResponse:
        colour = "#1a7f37" if ok else "#c62828"
        body = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                f"<title>Kairo</title><body style='font:18px system-ui;max-width:640px;margin:40px auto;padding:0 16px'>"
                f"<h1 style='color:{colour}'>{_esc(title)}</h1><p>{_esc(text)}</p>")
        return HTMLResponse(body, status_code=200 if ok else 400)

    @app.get("/setup/google/start")
    def google_start(request: Request, kind: str):
        if not is_local_request(request):
            raise HTTPException(403, "Google sign-in only on the PC itself")
        try:
            return RedirectResponse(app.state.oauth.start_url(kind), status_code=302)
        except GoogleNotConnected as exc:
            return _page("Google is not set up yet", str(exc), ok=False)
        except OAuthError as exc:
            return _page("Can't start", str(exc), ok=False)

    @app.get("/setup/google/callback")
    def google_callback(request: Request, state: str = "", code: str = "", error: str = ""):
        if not is_local_request(request):
            raise HTTPException(403, "Google sign-in only on the PC itself")
        if error:
            return _page("Not signed in", f"Google said: {error}. Go back to the setup window and try again.", ok=False)
        try:
            result = app.state.oauth.finish(state=state, code=code)
        except (OAuthError, GoogleNotConnected) as exc:
            return _page("Not signed in", str(exc), ok=False)
        with session_for(engine) as s:
            audit(s, actor="owner", action=f"google.signin.{result['kind']}", outcome=f"signed in as {result['email']}")
        what = "reading" if result["kind"] == "read" else "drafts, labels and approved sends"
        return _page("Done", f"Kairo can now use {result['email']} for {what}. Close this tab and go back to the setup window.", ok=True)

    @app.get("/api/setup/google/status")
    def google_status():
        return app.state.oauth.status()

    # ---------------------------------------------------------------- push (owner's devices)

    @app.get("/api/push/key")
    def push_key():
        return {"publicKey": app.state.pusher.public_key}

    @app.post("/api/push/subscribe")
    def push_subscribe(body: dict):
        try:
            app.state.pusher.subscribe(str(body.get("endpoint") or ""), body.get("keys") or {}, body.get("device"))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        with session_for(engine) as s:
            audit(s, actor="owner", action="push.subscribe", outcome=f"device {body.get('device') or '?'} added")
        return {"ok": True}

    @app.get("/api/push/devices")
    def push_devices():
        from .db import PushSubscription
        with session_for(engine) as s:
            return [{"id": r.id, "device": r.device} for r in s.exec(select(PushSubscription)).all()]

    @app.post("/api/push/test")
    def push_test():
        app.state.pusher.notify("Kairo", "Notifications work.", "/#more", ignore_quiet=True)
        return {"ok": True}

    # ---------------------------------------------------------------- approvals (owner's app)

    @app.get("/api/approvals")
    def list_approvals(status: str = "open", limit: int = 50):
        with session_for(engine) as s:
            approvals.expire_old(s)
            q = select(Approval).order_by(Approval.id.desc()).limit(min(limit, 200))
            if status != "all":
                q = q.where(Approval.status == status)
            return [approvals.card_dict(c) for c in s.exec(q).all()]

    @app.post("/api/approvals/{card_id}/approve")
    def approve_card(card_id: int, body: dict | None = None):
        body = body or {}
        with session_for(engine) as s:
            try:
                card = approvals.approve(s, card_id, executor=app.state.executor, gate=gate,
                                         confirm=bool(body.get("confirm")), edited_args=body.get("args"))
            except approvals.ApprovalError as exc:
                raise HTTPException(409, str(exc))
            return approvals.card_dict(card)

    @app.post("/api/approvals/{card_id}/reject")
    def reject_card(card_id: int):
        with session_for(engine) as s:
            try:
                return approvals.card_dict(approvals.reject(s, card_id))
            except approvals.ApprovalError as exc:
                raise HTTPException(409, str(exc))

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
        if not same_origin(ws.headers.get("origin"), ws.headers.get("host"), ws.headers.get("x-forwarded-host")):
            await ws.close(code=1008)
            return
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
            if killswitch.is_paused(s):
                await ws.send_json({"type": "error", "status": "paused",
                                    "message": "Kairo is paused (kill switch). Resume it under More."})
                return
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
        async for event in runner.stream(text_in, session_id=session_id, system=system_prompt, tools=model_tools,
                                         origin="owner_chat", thread_id=tid):
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
                status = "paused" if error.data.get("cancelled") else (error.status or BrainStatus.unavailable).value
                chat.add_message(s, tid, "system", f"[{status}] {error.text}")
                await ws.send_json({"type": "error", "message": error.text, "status": status})
                return
            msg = chat.add_message(s, tid, "kairo", final_text or "")
        await ws.send_json({"type": "done", "text": final_text or "", "message_id": msg.id})

    @app.post("/api/selftest")
    async def selftest(request: Request):
        """One tiny brain call, to prove the Claude login works for the account Kairo runs as.
        Only from this PC (boot check, installer): not reachable through Tailscale."""
        if not is_local_request(request):
            raise HTTPException(403, "self-test only from this PC")
        started = time.monotonic()
        reply, error = "", None
        async for event in runner.stream(
            "Reply with exactly: OK", session_id=None,
            system="You are a health check. Reply with exactly: OK", tools=[],
        ):
            if event.kind is EventKind.result:
                reply = event.text
            elif event.kind is EventKind.error:
                error = event
        status = runner.brain.health().value
        ok = error is None and "OK" in reply.upper()
        return {"ok": ok, "brain": status, "reply": reply.strip()[:200],
                "error": error.text[:500] if error else None,
                "seconds": round(time.monotonic() - started, 1)}

    @app.get("/api/status")
    def status():
        """Last boot check (written by the boot-check task on kairo-home), for the More screen."""
        path = settings.data_dir / "logs" / "boot-check.json"
        boot = None
        if path.is_file():
            try:
                boot = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                boot = {"error": "boot-check.json unreadable"}
        return {"brain": runner.brain.health().value, "boot_check": boot}

    _mount_web(app, settings.web_dist_dir)
    return app


LOCAL_HOSTS = {"127.0.0.1", "::1"}


def _esc(text: str) -> str:
    import html
    return html.escape(text, quote=True)


def is_loopback(request: Request) -> bool:
    """Connected from this machine and not proxied by tailscale serve."""
    client = request.client.host if request.client else ""
    forwarded = any(h in request.headers for h in ("x-forwarded-for", "x-forwarded-host", "tailscale-user-login"))
    return client in LOCAL_HOSTS and not forwarded


def is_local_request(request: Request) -> bool:
    """Direct from this machine: loopback client, loopback Host, not proxied by
    `tailscale serve` (which connects from loopback but sets the tailnet Host
    and forwarding headers)."""
    client = request.client.host if request.client else ""
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
    forwarded = any(h in request.headers for h in ("x-forwarded-for", "x-forwarded-host", "tailscale-user-login"))
    return client in LOCAL_HOSTS and host in {"127.0.0.1", "localhost", "::1"} and not forwarded


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

