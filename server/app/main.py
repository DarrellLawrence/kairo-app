"""Kairo API: health, chat threads and the chat websocket. Serves the PWA too.

Run: uvicorn app.main:create_app --factory
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import os
import json
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import inspect, text
from sqlmodel import select

from . import approvals, backup, board, brief, capture, lerndoku, life, school, chat, killswitch, location, lookup, onboarding, reviews, triage, vault
from .scheduler import JOB_TITLES, Jobs
from .voice import NotInstalled, Voice, VoiceError
from .voice import calls as voice_calls
from .audit import audit
from .executor import Executor
from .gate import TOOL_PREFIX, load_gate
from .gate_context import read_context
from .housekeeping import Housekeeping, run_forever
from .google.client import GoogleAPI, GoogleError, GoogleNotConnected
from .google.oauth import WEB_CLIENT, GoogleOAuth, OAuthError
from .mcp_tools import tool_list, tool_names
from .push import Pusher, in_quiet_hours
from .tools import ToolRunner
from .workspace import write_workspace
from .brain import BrainStatus, EventKind, make_brain
from .db import Approval, AuditLog, ChatMessage, ChatThread, Review, SettingKV, make_engine, session_for
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
    base_prompt = render_personality(settings.config_dir, profile)
    vault.ensure_structure(settings.vault_dir)

    def system_prompt() -> str:
        """Personality + the vault's Profile and Goals, read fresh for every chat message."""
        return base_prompt + "\n\n" + vault.context(settings.vault_dir)

    if settings.db_url.startswith("sqlite:///"):
        Path(settings.db_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.db_url)
    gate = load_gate(settings.config_dir)   # refuses to start on a broken or weakened permissions.yaml
    tool_token = secrets.token_urlsafe(32)  # new every start; only mcp_kairo gets it
    mcp_config = write_workspace(settings.workspace_dir,
                                 tool_url=f"http://127.0.0.1:{settings.port}/internal/tools", token=tool_token)
    hook_env = {"KAIRO_HOOK_CONFIG_DIR": str(settings.config_dir), "KAIRO_HOOK_DB_URL": settings.db_url}
    runner = chat.BrainRunner(make_brain(settings, mcp_config=mcp_config, hook_env=hook_env))
    from . import models as model_table
    runner.on_usage = lambda purpose, usage, ok: model_table.record(engine, purpose, usage, ok)
    model_tools = tool_names(gate)

    def google_read() -> GoogleAPI:
        return GoogleAPI(settings.secrets_dir, "read", transport=getattr(app.state, "google_transport", None))

    tools = ToolRunner(engine=engine, vault_dir=settings.vault_dir, google_read=google_read)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(run_forever(Housekeeping(app)))
        if os.environ.get("KAIRO_SCHEDULER", "1") != "0":   # tests switch it off
            app.state.jobs.start()
        yield
        app.state.jobs.stop()
        task.cancel()
        engine.dispose()

    app = FastAPI(title="Kairo", lifespan=lifespan)
    app.state.settings = settings
    app.state.runner = runner
    app.state.system_prompt = system_prompt
    app.state.gate = gate
    app.state.tool_token = tool_token
    app.state.executor = Executor(secrets_dir=settings.secrets_dir)
    app.state.pusher = Pusher(engine=engine, secrets_dir=settings.secrets_dir, profile=profile)
    app.state.on_new_card = lambda card: app.state.pusher.notify(
        f"#{card.id} {'Delete' if card.tier >= 4 else 'Approve'}?", card.summary, "/#approvals")
    inbox_dir = settings.data_dir / "inbox"
    pusher = app.state.pusher
    app.state.triage = triage.Triage(
        engine=engine, gate=gate, executor=lambda: app.state.executor, google_read=google_read,
        on_flag=lambda m: pusher.notify("Suspicious mail flagged", f"{m.sender}: {m.subject}. Kairo did nothing with it.", "/#today"))
    app.state.brief = brief.BriefMaker(engine=engine, google_read=google_read, vault_dir=settings.vault_dir, profile=profile)
    app.state.voice = Voice(settings.data_dir / "models", site_dir=settings.data_dir.parent / "tools" / "voice")

    def _transcribe_for_capture(data: bytes) -> str:
        return app.state.voice.transcribe(data).text

    app.state.captures = capture.Processor(
        engine=engine, vault_dir=settings.vault_dir, profile=profile, transcribe=_transcribe_for_capture,
        on_question=lambda c: pusher.notify("Kairo has a question", c.question or "About your capture", "/#today"))
    backup_dir = settings.data_dir / "backups" / "nightly"

    def _backup_status(s, **update):
        row = s.get(SettingKV, "backup_status") or SettingKV(key="backup_status", value="{}")
        st = json.loads(row.value or "{}")
        st.update(update)
        row.value = json.dumps(st, default=str)
        s.add(row)
        s.commit()
        return st

    def backup_job(ctx):
        try:
            path = backup.make(db_path=settings.db_path, data_dir=settings.data_dir, config_dir=settings.config_dir,
                               dest_dir=backup_dir)
        except Exception as exc:
            _backup_status(ctx.session, last_error=f"{dt.datetime.now(profile.tz):%Y-%m-%d %H:%M}: {exc}")
            pusher.notify("Backup failed", str(exc)[:200], "/#more")
            raise
        backup.rotate(backup_dir)
        _backup_status(ctx.session, last_backup=path.name, last_backup_at=dt.datetime.now(profile.tz).isoformat(),
                       last_size=path.stat().st_size, last_error=None)
        return path.name

    def restore_test_job(ctx):
        z = backup.latest(backup_dir)
        if z is None:
            raise RuntimeError("no backup to test yet")
        try:
            result = backup.verify(z)
        except Exception as exc:
            _backup_status(ctx.session, test_ok=False, test_at=dt.datetime.now(profile.tz).isoformat(), test_detail=str(exc))
            pusher.notify("Restore test failed", str(exc)[:200], "/#more")
            raise
        _backup_status(ctx.session, test_ok=True, test_at=dt.datetime.now(profile.tz).isoformat(), test_detail=result)
        return json.dumps(result)

    def lerndoku_prompt_job(ctx):
        week = lerndoku.week_of(dt.datetime.now(profile.tz).date())
        row, created = lerndoku.ensure(ctx.session, week)
        if row.status == "waiting":
            pusher.notify("Lerndokumentation", "2 minutes: what did you do at work this week? Kairo writes the German draft.",
                          "/#lerndoku")
        return week

    app.state.jobs = Jobs(
        engine=engine, pusher=pusher, profile=profile, schedule=_load_schedule(settings.config_dir),
        ask=lambda prompt, system, images=None, purpose="summary", model=None: chat.ask_sync(
            runner, prompt, system=system, images=images, purpose=purpose, model=model),
        bodies={"email_triage": lambda ctx: app.state.triage.run(ctx),
                "morning_brief": lambda ctx, day=None: app.state.brief.make(ctx, day),
                "capture_process": lambda ctx: app.state.captures.run(ctx),
                "backup": backup_job, "restore_test": restore_test_job,
                "lerndoku_prompt": lambda ctx: lerndoku_prompt_job(ctx)})
    app.state.maintenance_until = 0.0   # set while the auto-updater installs a new version
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

    def _page(title: str, text: str, ok: bool, link: tuple[str, str] | None = None) -> HTMLResponse:
        colour = "#1a7f37" if ok else "#c62828"
        body = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                f"<title>Kairo</title><body style='font:18px system-ui;max-width:640px;margin:40px auto;padding:0 16px'>"
                f"<h1 style='color:{colour}'>{_esc(title)}</h1><p>{_esc(text)}</p>")
        if link:
            body += (f"<p><a href='{_esc(link[0])}' style='display:inline-block;padding:14px 20px;background:#1a73e8;"
                     f"color:#fff;border-radius:10px;text-decoration:none'>{_esc(link[1])}</a></p>")
        return HTMLResponse(body, status_code=200 if ok else 400)

    def _phone_redirect(request: Request) -> str:
        """Sign-in from the iPhone: only through Kairo's own tailnet name (tailscale serve)."""
        host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        if not host.endswith(".ts.net"):
            raise HTTPException(403, "Google sign-in only on the PC or through Kairo's tailnet address")
        return f"https://{host}/setup/google/callback"

    @app.get("/setup/google/start")
    def google_start(request: Request, kind: str):
        try:
            if is_local_request(request):
                return RedirectResponse(app.state.oauth.start_url(kind), status_code=302)
            redirect = _phone_redirect(request)
            if not app.state.oauth.has_web_client():
                return _page("Not set up for the phone yet",
                             "Signing in again from the iPhone needs a one-time step on the PC: run Kairo Logins "
                             "(Google part). Until then, sign in again with Kairo Logins on the PC.", ok=False)
            return RedirectResponse(app.state.oauth.start_url(kind, redirect_uri=redirect, client_file=WEB_CLIENT),
                                    status_code=302)
        except GoogleNotConnected as exc:
            return _page("Google is not set up yet", str(exc), ok=False)
        except OAuthError as exc:
            return _page("Can't start", str(exc), ok=False)

    @app.get("/setup/google/callback")
    def google_callback(request: Request, state: str = "", code: str = "", error: str = ""):
        phone = not is_local_request(request)
        redirect = _phone_redirect(request) if phone else None
        if error:
            return _page("Not signed in", f"Google said: {error}. Start the sign-in again.", ok=False)
        try:
            result = app.state.oauth.finish(state=state, code=code, redirect_uri=redirect)
        except (OAuthError, GoogleNotConnected) as exc:
            return _page("Not signed in", str(exc), ok=False)
        with session_for(engine) as s:
            audit(s, actor="owner", action=f"google.signin.{result['kind']}",
                  outcome=f"signed in as {result['email']}" + (" (from the phone)" if phone else ""))
        what = "reading" if result["kind"] == "read" else "drafts, labels and approved sends"
        if phone and result["kind"] == "read":
            return _page("Step 1 of 2 done", f"Kairo can read {result['email']} again. One more sign-in for the "
                         "write permission (drafts, labels, approved sends):", ok=True,
                         link=("/setup/google/start?kind=write", "Step 2 of 2: sign in for write"))
        if phone:
            return _page("Done", f"Kairo can use {result['email']} again for {what}. You can close this tab.", ok=True)
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
        if time.time() < app.state.maintenance_until:
            await ws.send_json({"type": "error", "status": "updating",
                                "message": "Kairo is installing an update. Try again in a minute."})
            return
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
        chat_purpose = "call" if text_in.startswith("(spoken, on a call)") else "chat"   # calls want speed: Haiku

        parts: list[str] = []
        final_text: str | None = None
        error = None
        new_sid = None
        async for event in runner.stream(text_in, session_id=session_id, system=system_prompt(), tools=model_tools,
                                         origin="owner_chat", thread_id=tid, purpose=chat_purpose,
                                         model=model_table.pick(chat_purpose, app.state.jobs.schedule)):
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
            system="You are a health check. Reply with exactly: OK", tools=[], model="haiku", purpose="selftest",
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

    # ---------------------------------------------------------------- auto-update (installer/Kairo-AutoUpdate.ps1)

    @app.post("/api/update/ready")
    def update_ready(request: Request, body: dict | None = None):
        """May the auto-updater restart Kairo now? Only when no chat or job is running, and not in
        the quiet hours unless the update is urgent. A yes holds new chats off for 30 minutes.
        Jobs (Milestone C) must check app.state.maintenance_until too."""
        if not is_local_request(request):
            raise HTTPException(403, "only from this PC")
        urgent = bool((body or {}).get("urgent"))
        if runner.busy:
            return {"ready": False, "reason": "busy"}
        if not urgent and in_quiet_hours(profile):
            return {"ready": False, "reason": "quiet"}
        app.state.maintenance_until = time.time() + 1800
        return {"ready": True, "reason": "idle"}

    @app.post("/api/update/release")
    def update_release(request: Request):
        if not is_local_request(request):
            raise HTTPException(403, "only from this PC")
        app.state.maintenance_until = 0.0
        return {"ok": True}

    # ---------------------------------------------------------------- onboarding interview (v5)

    @app.get("/api/onboarding")
    def onboarding_state():
        with session_for(engine) as s:
            return onboarding.state(s)

    @app.put("/api/onboarding/{question_id}")
    def onboarding_answer(question_id: str, body: dict):
        with session_for(engine) as s:
            try:
                return onboarding.save(s, settings.vault_dir, question_id, str(body.get("value") or ""))
            except (onboarding.OnboardingError, vault.VaultError) as exc:
                raise HTTPException(400, str(exc))

    @app.post("/api/onboarding/reset")
    def onboarding_reset():
        with session_for(engine) as s:
            return onboarding.reset(s, settings.vault_dir)

    # ---------------------------------------------------------------- reviews (v5)

    @app.get("/api/reviews")
    def list_reviews(status: str = "open", limit: int = 30):
        with session_for(engine) as s:
            q = select(Review).order_by(Review.period_start.desc(), Review.id.desc()).limit(min(limit, 200))
            if status != "all":
                q = q.where(Review.status == status)
            return [reviews.as_dict(r) for r in s.exec(q).all()]

    @app.get("/api/reviews/{review_id}")
    def get_review(review_id: int):
        with session_for(engine) as s:
            r = s.get(Review, review_id)
            if r is None:
                raise HTTPException(404, "no such review")
            return reviews.as_dict(r)

    @app.post("/api/reviews/{review_id}/complete")
    def complete_review(review_id: int, body: dict):
        with session_for(engine) as s:
            try:
                r = reviews.complete(s, settings.vault_dir, review_id, dict(body.get("answers") or {}))
            except (reviews.ReviewError, vault.VaultError) as exc:
                raise HTTPException(400, str(exc))
            audit(s, actor="owner", action=f"review.{r.kind}", outcome=f"done; note {r.vault_path}")
            return reviews.as_dict(r)

    @app.post("/api/reviews/{review_id}/skip")
    def skip_review(review_id: int):
        with session_for(engine) as s:
            try:
                return reviews.as_dict(reviews.skip(s, review_id))
            except reviews.ReviewError as exc:
                raise HTTPException(400, str(exc))

    @app.post("/api/reviews/start")
    def start_review(body: dict):
        """Any review on demand (KICKOFF 12: every job runnable on demand)."""
        kind = str(body.get("kind") or "")
        with session_for(engine) as s:
            try:
                r, _ = reviews.create(s, kind, dt.datetime.now(profile.tz).date(), profile.tz)
            except reviews.ReviewError as exc:
                raise HTTPException(400, str(exc))
            return reviews.as_dict(r)

    # ---------------------------------------------------------------- jobs (Milestone C)

    @app.get("/api/jobs")
    def jobs_list():
        rows = app.state.jobs.last_runs()
        return {"jobs": [r for r in rows if "name" in r], "brain": next(r["brain"] for r in rows if "brain" in r)}

    @app.post("/api/jobs/{name}/run")
    def jobs_run(name: str):
        if name not in JOB_TITLES or name not in app.state.jobs.bodies:
            raise HTTPException(404, "no such job")
        threading.Thread(target=app.state.jobs.run_job, args=(name,), kwargs={"forced": True},
                         name=f"job-{name}", daemon=True).start()
        return {"started": name}

    @app.get("/api/brief")
    def brief_get(date: str | None = None):
        try:
            day = dt.date.fromisoformat(date) if date else dt.datetime.now(profile.tz).date()
        except ValueError:
            raise HTTPException(400, "date must be YYYY-MM-DD")
        with session_for(engine) as s:
            return {"brief": brief.latest(s, day)}

    @app.get("/api/triage")
    def triage_get():
        with session_for(engine) as s:
            return {"needs_reply": [triage.as_dict(m) for m in triage.needs_reply(s, limit=20)],
                    "flagged": [triage.as_dict(m) for m in triage.flagged(s, days=7)]}

    @app.post("/api/triage/{message_id}/done")
    def triage_done(message_id: str):
        with session_for(engine) as s:
            row = s.get(triage.TriagedEmail, message_id)
            if row is None:
                raise HTTPException(404, "unknown mail")
            row.done = True
            s.add(row)
            s.commit()
            return triage.as_dict(row)

    # ---------------------------------------------------------------- capture (Milestone C)

    async def _capture(request: Request, source: str):
        ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        body = await request.body()
        with session_for(engine) as s:
            try:
                if ctype in ("application/json", "text/plain", ""):
                    if ctype == "application/json":
                        try:
                            data = json.loads(body or b"{}")
                        except ValueError:
                            raise HTTPException(400, "body is not JSON")
                        data = data if isinstance(data, dict) else {"text": str(data)}
                        text_in, url_in = data.get("text"), data.get("url")
                    else:
                        text_in, url_in = body.decode("utf-8", errors="replace"), None
                    cap = capture.store_text(s, inbox_dir, profile, text=str(text_in or ""), url=str(url_in or ""), source=source)
                else:
                    cap = capture.store_file(s, inbox_dir, profile, data=body, content_type=ctype,
                                             note=request.query_params.get("note"), source=source)
            except capture.CaptureError as exc:
                raise HTTPException(400, str(exc))
            audit(s, actor="owner", action="capture", outcome=f"stored #{cap.id} ({cap.kind}, {source})")
            return {"ok": True, "id": cap.id, "kind": cap.kind, "message": "Captured."}

    @app.post("/api/capture")
    async def capture_from_shortcut(request: Request):
        """The iPhone Shortcut. Needs the capture token (Authorization: Bearer ...)."""
        if not capture.check_token(settings.secrets_dir, request.headers.get("authorization")):
            raise HTTPException(401, "wrong or missing capture token")
        source = request.query_params.get("source") or "shortcut"
        return await _capture(request, source if source in ("shortcut", "share") else "shortcut")

    @app.post("/api/captures")
    async def capture_from_app(request: Request):
        return await _capture(request, "app")

    @app.get("/api/captures")
    def captures_list(status: str | None = None, limit: int = 50):
        with session_for(engine) as s:
            q = select(capture.Capture).order_by(capture.Capture.id.desc()).limit(min(limit, 200))
            if status:
                q = q.where(capture.Capture.status == status)
            return [capture.as_dict(c) for c in s.exec(q).all()]

    @app.post("/api/captures/{capture_id}/answer")
    def captures_answer(capture_id: int, body: dict):
        with session_for(engine) as s:
            try:
                cap = capture.answer(s, capture_id, str(body.get("text") or ""))
            except capture.CaptureError as exc:
                raise HTTPException(400, str(exc))
        threading.Thread(target=app.state.jobs.run_job, args=("capture_process",), kwargs={"forced": True}, daemon=True).start()
        return capture.as_dict(cap)

    @app.post("/api/captures/{capture_id}/dismiss")
    def captures_dismiss(capture_id: int):
        with session_for(engine) as s:
            try:
                return capture.as_dict(capture.dismiss(s, capture_id))
            except capture.CaptureError as exc:
                raise HTTPException(404, str(exc))

    @app.get("/api/capture/setup")
    def capture_setup(request: Request):
        """For More -> Capture: what to put into the iPhone Shortcut."""
        host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
        return {"url": f"https://{host}/api/capture" if host.endswith(".ts.net") else None,
                "token": capture.token(settings.secrets_dir)}

    @app.post("/api/capture/token/rotate")
    def capture_token_rotate():
        with session_for(engine) as s:
            audit(s, actor="owner", action="capture.token_rotate", outcome="new token")
        return {"token": capture.rotate_token(settings.secrets_dir)}

    # ---------------------------------------------------------------- location (privacy first)

    app.state.lookup = lookup.Lookup(timezone=profile.timezone)

    @app.post("/api/location/event")
    def location_event(request: Request, body: dict):
        """iPhone Shortcut automation: arrive/leave a named place. Needs the location token."""
        if not location.check_token(settings.secrets_dir, request.headers.get("authorization")):
            raise HTTPException(401, "wrong or missing location token")
        ts = None
        if body.get("time"):
            try:
                ts = dt.datetime.fromisoformat(str(body["time"]).replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(400, "time must be ISO 8601, e.g. 2026-10-14T07:42:00+02:00")
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=profile.tz)
        with session_for(engine) as s:
            try:
                row = location.record(s, place=str(body.get("place") or ""), event=str(body.get("event") or "").lower(), ts=ts)
            except location.LocationError as exc:
                raise HTTPException(400, str(exc))
            if row is None:
                return {"ok": True, "stored": False, "paused": location.is_paused(s)}
            if row.event == "arrive":
                msg = location.arrival_reminder(s, row.place)
                if msg:
                    app.state.pusher.notify(msg[0], msg[1], "/#today")
            return {"ok": True, "stored": True, "id": row.id}

    @app.get("/api/location")
    def location_get(request: Request, limit: int = 200):
        host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
        with session_for(engine) as s:
            return {"paused": location.is_paused(s), "now": location.current_place(s), "places": location.places(s),
                    "history": [location.as_dict(e) for e in location.history(s, min(limit, 1000))],
                    "patterns": location.patterns(s, profile.tz), "keep_days": location.KEEP_DAYS,
                    "url": f"https://{host}/api/location/event" if host.endswith(".ts.net") else None,
                    "token": location.token(settings.secrets_dir)}

    @app.post("/api/location/pause")
    def location_pause(body: dict):
        with session_for(engine) as s:
            location.set_paused(s, bool(body.get("paused")))
            audit(s, actor="owner", action="location.pause" if body.get("paused") else "location.resume", outcome="set")
            return {"paused": location.is_paused(s)}

    @app.delete("/api/location/events/{event_id}")
    def location_delete(event_id: int):
        with session_for(engine) as s:
            if not location.delete_one(s, event_id):
                raise HTTPException(404, "no such event")
            return {"deleted": 1}

    @app.delete("/api/location/events")
    def location_delete_all():
        with session_for(engine) as s:
            n = location.delete_all(s)
            audit(s, actor="owner", action="location.delete_all", outcome=f"deleted {n}")
            return {"deleted": n}

    @app.post("/api/location/token/rotate")
    def location_token_rotate():
        return {"token": location.rotate_token(settings.secrets_dir)}

    @app.post("/api/location/lookup")
    def location_lookup(body: dict):
        """Only when the owner taps "use my location": weather + nearest departures. Not stored, not logged."""
        try:
            lat, lon = lookup.check_coords(body.get("lat"), body.get("lon"))
        except lookup.LookupError_ as exc:
            raise HTTPException(400, str(exc))
        out: dict = {}
        want = body.get("want") or ["weather", "transport"]
        try:
            if "weather" in want:
                out["weather"] = app.state.lookup.weather(lat, lon)
        except Exception as exc:
            out["weather_error"] = f"Weather not available ({type(exc).__name__})"
        try:
            if "transport" in want:
                out["stations"] = app.state.lookup.stations(lat, lon)
        except Exception as exc:
            out["transport_error"] = f"Timetable not available ({type(exc).__name__})"
        return out

    # ---------------------------------------------------------------- board (Milestone D)

    from .db import Goal, Project, Task as TaskRow
    from pydantic import ValidationError

    def _parse(model, body):
        try:
            return model.model_validate(body or {})
        except ValidationError as exc:
            raise HTTPException(400, "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:3]))

    @app.get("/api/board")
    def board_get():
        with session_for(engine) as s:
            return board.board(s)

    for _path, _row, _in, _patch, _dict in (
        ("goals", Goal, board.GoalIn, board.GoalPatch, board.goal_dict),
        ("projects", Project, board.ProjectIn, board.ProjectPatch, board.project_dict),
        ("tasks", TaskRow, board.TaskIn, board.TaskPatch, board.task_dict),
    ):
        def _make(row=_row, in_=_in, patch=_patch, to_dict=_dict, path=_path):
            def create_(body: dict):
                with session_for(engine) as s:
                    try:
                        return to_dict(board.create(s, row, _parse(in_, body)))
                    except board.BoardError as exc:
                        raise HTTPException(400, str(exc))

            def update_(item_id: int, body: dict):
                with session_for(engine) as s:
                    try:
                        return to_dict(board.update(s, row, item_id, _parse(patch, body)))
                    except board.BoardError as exc:
                        raise HTTPException(404 if str(exc).startswith(f"no {row.__tablename__[:-1]} ") else 400, str(exc))

            app.post(f"/api/{path}", name=f"{path}_create")(create_)
            app.patch(f"/api/{path}/{{item_id}}", name=f"{path}_update")(update_)
        _make()

    # ---------------------------------------------------------------- school (Milestone D)

    from .db import Flashcard, Grade, SchoolEvent as SchoolEventRow

    def _today():
        return dt.datetime.now(profile.tz).date()

    def _model_ask(prompt, system, purpose="study"):
        try:
            return app.state.jobs.ask_brain(prompt, system, purpose=purpose)
        except Exception as exc:   # budget reached / usage limit / no model
            raise HTTPException(503, f"Claude is not available right now: {exc}")

    @app.get("/api/school")
    def school_get():
        with session_for(engine) as s:
            return school.overview(s, _today(), profile.tz)

    @app.post("/api/school/events")
    def school_event_add(body: dict):
        data = _parse(school.EventIn, body)
        with session_for(engine) as s:
            d = data.date if data.date.tzinfo else data.date.replace(tzinfo=profile.tz)
            row = SchoolEventRow(subject=data.subject.strip(), kind=data.kind, date=d, notes=data.notes)
            s.add(row)
            s.commit()
            s.refresh(row)
            return {"id": row.id}

    @app.delete("/api/school/events/{event_id}")
    def school_event_delete(event_id: int):
        with session_for(engine) as s:
            row = s.get(SchoolEventRow, event_id)
            if row is None:
                raise HTTPException(404, "no such event")
            s.delete(row)
            s.commit()
            return {"deleted": 1}

    @app.post("/api/school/grades")
    def school_grade_add(body: dict):
        data = _parse(school.GradeIn, body)
        with session_for(engine) as s:
            row = Grade(subject=data.subject.strip(), grade=data.grade, weight=data.weight, date=data.date or _today(), title=data.title)
            s.add(row)
            s.commit()
            s.refresh(row)
            return {"id": row.id}

    @app.delete("/api/school/grades/{grade_id}")
    def school_grade_delete(grade_id: int):
        with session_for(engine) as s:
            row = s.get(Grade, grade_id)
            if row is None:
                raise HTTPException(404, "no such grade")
            s.delete(row)
            s.commit()
            return {"deleted": 1}

    @app.get("/api/school/needed")
    def school_needed(subject: str, target: float = 4.0, weight: float = 1.0):
        if not (1 <= target <= 6) or not (0 < weight <= 10):
            raise HTTPException(400, "target 1-6, weight above 0")
        with session_for(engine) as s:
            grades = s.exec(select(Grade).where(Grade.subject == subject)).all()
            return {"subject": subject, "target": target, "weight": weight, "needed": school.needed(list(grades), target, weight)}

    @app.post("/api/school/guide")
    def school_guide(body: dict):
        data = _parse(school.MaterialIn, body)
        try:
            return school.make_guide(_model_ask, settings.vault_dir, data)
        except school.SchoolError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/school/cards/generate")
    def school_cards_generate(body: dict):
        data = _parse(school.MaterialIn, body)
        with session_for(engine) as s:
            try:
                cards = school.make_cards(_model_ask, s, settings.vault_dir, data, _today())
            except school.SchoolError as exc:
                raise HTTPException(400, str(exc))
            return [school.card_dict(c) for c in cards]

    @app.post("/api/school/cards")
    def school_card_add(body: dict):
        data = _parse(school.CardIn, body)
        with session_for(engine) as s:
            c = Flashcard(**data.model_dump(), box=1, due=_today())
            s.add(c)
            s.commit()
            s.refresh(c)
            return school.card_dict(c)

    @app.get("/api/school/cards/due")
    def school_cards_due(subject: str | None = None):
        with session_for(engine) as s:
            return [school.card_dict(c) for c in school.due_cards(s, _today(), subject)]

    @app.get("/api/school/cards")
    def school_cards_all(subject: str):
        with session_for(engine) as s:
            return [school.card_dict(c) for c in s.exec(select(Flashcard).where(Flashcard.subject == subject).order_by(Flashcard.id)).all()]

    @app.post("/api/school/cards/{card_id}/review")
    def school_card_review(card_id: int, body: dict):
        with session_for(engine) as s:
            c = s.get(Flashcard, card_id)
            if c is None:
                raise HTTPException(404, "no such card")
            school.review(c, bool(body.get("knew")), _today())
            s.add(c)
            s.commit()
            s.refresh(c)
            return school.card_dict(c)

    @app.delete("/api/school/cards/{card_id}")
    def school_card_delete(card_id: int):
        with session_for(engine) as s:
            c = s.get(Flashcard, card_id)
            if c is None:
                raise HTTPException(404, "no such card")
            s.delete(c)
            s.commit()
            return {"deleted": 1}

    # ---------------------------------------------------------------- Lerndokumentation (Milestone D)

    def _week(week: str | None) -> str:
        if not week:
            return lerndoku.week_of(dt.datetime.now(profile.tz).date())
        try:
            lerndoku.week_range(week)
        except (ValueError, IndexError):
            raise HTTPException(400, "week must look like 2026-W42")
        return week

    @app.get("/api/lerndoku")
    def lerndoku_get(week: str | None = None):
        wk = _week(week)
        with session_for(engine) as s:
            row = s.get(lerndoku.Lerndoku, wk)
            current = lerndoku.as_dict(row, settings.vault_dir) if row else {
                "week": wk, "status": "none", "notes": "", "draft": None, "vault_path": None,
                **dict(zip(("start", "end"), (d.isoformat() for d in lerndoku.week_range(wk))))}
            return {"current": current, "weeks": lerndoku.listing(s, settings.vault_dir)}

    @app.post("/api/lerndoku/{week}/notes")
    def lerndoku_notes(week: str, body: dict):
        wk = _week(week)
        with session_for(engine) as s:
            try:
                row = lerndoku.write_draft(lambda p, sy: _model_ask(p, sy, "lerndoku"), s, settings.vault_dir, wk, str(body.get("notes") or ""))
            except lerndoku.LerndokuError as exc:
                raise HTTPException(400, str(exc))
            return lerndoku.as_dict(row, settings.vault_dir)

    @app.put("/api/lerndoku/{week}")
    def lerndoku_save(week: str, body: dict):
        wk = _week(week)
        with session_for(engine) as s:
            try:
                return lerndoku.as_dict(lerndoku.save(s, settings.vault_dir, wk, str(body.get("draft") or "")), settings.vault_dir)
            except lerndoku.LerndokuError as exc:
                raise HTTPException(400, str(exc))

    @app.post("/api/lerndoku/{week}/approve")
    def lerndoku_approve(week: str, body: dict):
        wk = _week(week)
        with session_for(engine) as s:
            try:
                return lerndoku.as_dict(lerndoku.set_status(s, settings.vault_dir, wk, bool(body.get("approved", True))), settings.vault_dir)
            except lerndoku.LerndokuError as exc:
                raise HTTPException(400, str(exc))

    # ---------------------------------------------------------------- voice (phase 3)

    from fastapi import Response

    @app.get("/api/voice/status")
    def voice_status():
        with session_for(engine) as s:
            return {**app.state.voice.status(), "call_options": voice_calls.options(s),
                    "call_option_titles": voice_calls.OPTIONS}

    @app.post("/api/voice/transcribe")
    async def voice_transcribe(request: Request):
        ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not ctype.startswith(("audio/", "video/")):   # Safari records audio/mp4
            raise HTTPException(400, "send the recording as the request body (audio/*)")
        body = await request.body()
        lang = request.query_params.get("language") or None
        try:
            t = await asyncio.to_thread(app.state.voice.transcribe, body, lang)
        except NotInstalled as exc:
            raise HTTPException(503, str(exc))
        except VoiceError as exc:
            raise HTTPException(400, str(exc))
        return {"text": t.text, "language": t.language, "seconds": t.seconds, "took": t.took}

    @app.post("/api/voice/speak")
    async def voice_speak(body: dict):
        text = str(body.get("text") or "")
        lang = body.get("language") if body.get("language") in ("de", "en") else None
        try:
            wav, used = await asyncio.to_thread(app.state.voice.speak, text, lang)
        except NotInstalled as exc:
            raise HTTPException(503, str(exc))
        except VoiceError as exc:
            raise HTTPException(400, str(exc))
        return Response(content=wav, media_type="audio/wav", headers={"X-Kairo-Language": used, "Cache-Control": "no-store"})

    @app.post("/api/voice/calls")
    def voice_call_now(body: dict):
        """'Call me now' (also how the owner tests it)."""
        reason = str(body.get("reason") or "Test call")[:120]
        with session_for(engine) as s:
            c = voice_calls.start(s, reason, f"Hi, it's Kairo. {reason}. What's on your mind?", app.state.pusher, ignore_quiet=True)
            return voice_calls.as_dict(c)

    @app.post("/api/voice/selftest")
    async def voice_selftest():
        """More -> Voice -> Test: speak a sentence, then recognise it again, on the PC."""
        def run():
            wav, lang = app.state.voice.speak("Kairo voice test. Please call the dentist tomorrow.", "en")
            t = app.state.voice.transcribe(wav, "en")
            return {"ok": "dentist" in t.text.lower(), "heard": t.text, "took": t.took,
                    "device": app.state.voice.status()["stt_device"]}
        try:
            return await asyncio.to_thread(run)
        except NotInstalled as exc:
            raise HTTPException(503, str(exc))
        except VoiceError as exc:
            return {"ok": False, "heard": "", "error": str(exc)}

    @app.get("/api/voice/calls")
    def voice_calls_list():
        with session_for(engine) as s:
            return [voice_calls.as_dict(c) for c in voice_calls.recent(s)]

    @app.get("/api/voice/calls/{call_id}")
    def voice_call_get(call_id: int):
        with session_for(engine) as s:
            c = voice_calls.get(s, call_id)
            if c is None:
                raise HTTPException(404, "no such call")
            return voice_calls.as_dict(c)

    @app.post("/api/voice/calls/{call_id}/{action}")
    def voice_call_action(call_id: int, action: str):
        if action not in ("answer", "end"):
            raise HTTPException(404, "unknown action")
        with session_for(engine) as s:
            try:
                return voice_calls.as_dict(voice_calls.set_status(s, call_id, "answered" if action == "answer" else "ended"))
            except KeyError:
                raise HTTPException(404, "no such call")

    @app.post("/api/voice/options/{key}")
    def voice_option(key: str, body: dict):
        with session_for(engine) as s:
            try:
                return voice_calls.set_option(s, key, bool(body.get("on")))
            except KeyError:
                raise HTTPException(404, "unknown option")

    # ---------------------------------------------------------------- phase 3: life (lists, people, money, health)

    from .db import Chore, Person, Transaction

    def _shortcut_auth(request: Request) -> None:
        if not capture.check_token(settings.secrets_dir, request.headers.get("authorization")):
            raise HTTPException(401, "wrong or missing Shortcut key")

    @app.get("/api/shopping")
    def shopping_get():
        with session_for(engine) as s:
            rows = s.exec(select(life.ShoppingItem).order_by(life.ShoppingItem.done, life.ShoppingItem.id)).all()
            return [{"id": r.id, "text": r.text, "done": r.done, "source": r.source} for r in rows]

    @app.post("/api/shopping")
    def shopping_post(body: dict):
        items = body.get("items")
        if not isinstance(items, list) or not items:
            raise HTTPException(400, "items: a list of things to buy")
        with session_for(engine) as s:
            return [{"id": r.id, "text": r.text} for r in life.shopping_add(s, [str(i) for i in items][:30])]

    @app.post("/api/shopping/{item_id}/toggle")
    def shopping_toggle(item_id: int):
        with session_for(engine) as s:
            try:
                r = life.shopping_toggle(s, item_id)
            except KeyError:
                raise HTTPException(404, "no such item")
            return {"id": r.id, "text": r.text, "done": r.done}

    @app.post("/api/shopping/clear-done")
    def shopping_clear():
        with session_for(engine) as s:
            return {"removed": life.shopping_clear_done(s)}

    @app.get("/api/chores")
    def chores_get():
        today = _today()
        with session_for(engine) as s:
            return [life.chore_dict(c, today) for c in s.exec(select(Chore).order_by(Chore.title)).all()]

    @app.post("/api/chores")
    def chores_post(body: dict):
        title = str(body.get("title") or "").strip()[:120]
        try:
            every = int(body.get("every_days") or 7)
        except (TypeError, ValueError):
            every = 0
        if not title or not 1 <= every <= 365:
            raise HTTPException(400, "title and every_days (1-365)")
        with session_for(engine) as s:
            c = Chore(title=title, every_days=every)
            s.add(c)
            s.commit()
            s.refresh(c)
            return life.chore_dict(c, _today())

    @app.post("/api/chores/{chore_id}/done")
    def chores_done(chore_id: int):
        with session_for(engine) as s:
            try:
                return life.chore_dict(life.chore_done(s, chore_id, _today()), _today())
            except KeyError:
                raise HTTPException(404, "no such chore")

    @app.delete("/api/chores/{chore_id}")
    def chores_delete(chore_id: int):
        with session_for(engine) as s:
            c = s.get(Chore, chore_id)
            if c is None:
                raise HTTPException(404, "no such chore")
            s.delete(c)
            s.commit()
            return {"deleted": 1}

    @app.get("/api/people")
    def people_get():
        today = _today()
        with session_for(engine) as s:
            return {"people": [life.person_dict(p, today) for p in life.people_list(s)], **life.people_reminders(s, today)}

    @app.post("/api/people")
    def people_post(body: dict):
        name = str(body.get("name") or "").strip()
        if not name or len(name) > 80:
            raise HTTPException(400, "name (1-80 characters)")
        with session_for(engine) as s:
            p = life.person_find(s, name) or Person(name=name)
            if "relation" in body:
                p.relation = (str(body["relation"] or "").strip()[:60]) or None
            if "birthday" in body:
                try:
                    p.birthday = dt.date.fromisoformat(body["birthday"]) if body["birthday"] else None
                except ValueError:
                    raise HTTPException(400, "birthday: YYYY-MM-DD")
            if "contact_every_days" in body:
                v = body["contact_every_days"]
                p.contact_every_days = int(v) if v not in (None, "", 0) and 1 <= int(v) <= 365 else None
            if "notes" in body:
                p.notes = (str(body["notes"] or "")[:20_000]) or None
            if body.get("contacted"):
                p.last_contact = _today()
            s.add(p)
            s.commit()
            s.refresh(p)
            return life.person_dict(p, _today())

    @app.delete("/api/people/{person_id}")
    def people_delete(person_id: int):
        with session_for(engine) as s:
            p = s.get(Person, person_id)
            if p is None:
                raise HTTPException(404, "no such person")
            s.delete(p)
            s.commit()
            return {"deleted": 1}

    @app.post("/api/finance/transaction")
    def finance_transaction(request: Request, body: dict):
        """Apple Pay Wallet 'Transaction' automation (Shortcut key). Spending is stored as a negative amount."""
        _shortcut_auth(request)
        try:
            amount = -abs(life.parse_amount(body.get("amount")))
        except ValueError:
            raise HTTPException(400, "amount missing")
        with session_for(engine) as s:
            t = life.add_transaction(s, ts=dt.datetime.now(dt.timezone.utc), amount=amount,
                                     currency=str(body.get("currency") or "CHF"), merchant=str(body.get("merchant") or "") or None,
                                     card=str(body.get("card") or "") or None, source="wallet")
            return {"ok": True, "stored": t is not None, "category": t.category if t else None}

    @app.post("/api/finance/import")
    async def finance_import(request: Request):
        """UBS e-banking export (camt.053 XML), uploaded from the app."""
        body = await request.body()
        with session_for(engine) as s:
            try:
                r = life.import_camt053(s, body)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            audit(s, actor="owner", action="finance.import", outcome=f"added {r['added']}, skipped {r['skipped']}")
            return r

    @app.get("/api/finance")
    def finance_get(days: int = 30):
        with session_for(engine) as s:
            return life.money_summary(s, max(1, min(days, 366)))

    @app.post("/api/finance/{tx_id}/category")
    def finance_category(tx_id: int, body: dict):
        cat = str(body.get("category") or "").strip().lower()[:40]
        if not cat:
            raise HTTPException(400, "category")
        with session_for(engine) as s:
            t = s.get(Transaction, tx_id)
            if t is None:
                raise HTTPException(404, "no such payment")
            t.category = cat
            s.add(t)
            s.commit()
            return {"id": t.id, "category": t.category}

    @app.post("/api/health")
    def health_post(request: Request, body: dict):
        """Daily Apple Health export from a Shortcut (Shortcut key)."""
        _shortcut_auth(request)
        try:
            day = dt.date.fromisoformat(str(body["date"])[:10]) if body.get("date") else _today() - dt.timedelta(days=1)
        except ValueError:
            raise HTTPException(400, "date: YYYY-MM-DD")
        with session_for(engine) as s:
            try:
                row = life.health_put(s, day, body)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            return {"ok": True, "date": row.date.isoformat()}

    @app.get("/api/health/summary")
    def health_get(days: int = 30):
        with session_for(engine) as s:
            return life.health_summary(s, max(1, min(days, 366)), _today())

    @app.get("/api/usage")
    def usage_get(days: int = 7):
        with session_for(engine) as s:
            return {**model_table.usage(s, max(1, min(days, 366))), "models": {p: model_table.pick(p, app.state.jobs.schedule)
                                                                              for p in model_table.DEFAULTS}}

    @app.get("/api/backups")
    def backups_list():
        with session_for(engine) as s:
            row = s.get(SettingKV, "backup_status")
            st = json.loads(row.value) if row and row.value else {}
        zips = sorted(backup_dir.glob("kairo-*.zip"), reverse=True) if backup_dir.is_dir() else []
        return {"status": st, "folder": str(backup_dir),
                "backups": [{"name": z.name, "size": z.stat().st_size} for z in zips]}

    # ---------------------------------------------------------------- owner's own to-dos (More tab)

    OWNER_TODOS = ["google_publish", "bios_power_on", "github_2fa"]

    @app.get("/api/owner-todos")
    def owner_todos():
        with session_for(engine) as s:
            return {t: (s.get(SettingKV, f"owner_todo:{t}") or SettingKV(key="", value="0")).value == "1" for t in OWNER_TODOS}

    @app.post("/api/owner-todos/{todo}")
    def owner_todo_set(todo: str, body: dict):
        if todo not in OWNER_TODOS:
            raise HTTPException(404, "unknown reminder")
        with session_for(engine) as s:
            row = s.get(SettingKV, f"owner_todo:{todo}") or SettingKV(key=f"owner_todo:{todo}")
            row.value = "1" if body.get("done") else "0"
            s.add(row)
            s.commit()
        return owner_todos()

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


def _load_schedule(config_dir: Path) -> dict:
    import yaml
    path = config_dir / "schedule.yaml"
    try:
        return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.is_file() else {}
    except (OSError, yaml.YAMLError):
        return {}


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

