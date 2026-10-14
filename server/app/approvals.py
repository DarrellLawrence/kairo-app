"""Approval cards for tier 3-4 actions (KICKOFF 8).

- A card stores the EXACT action + args the backend will run. For replies,
  deletes and responses the backend resolves the details (recipient, subject,
  thread, who gets notified) when the card is made, so what the owner approves
  is what gets sent.
- The summary is built by the backend from the stored args, never by the model.
- Only the owner's app can approve or reject (no tool for it exists).
- Edit-then-approve goes through the gate again. Tier 4 needs a second
  confirmation. Cards expire after 48 hours. The daily outbound limit is
  checked again at approval.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlmodel import Session, select

from . import killswitch
from .audit import audit
from .db import Approval, ChatMessage, utcnow
from .executor import Executor, first_address
from .gate import Decision, Gate, GateContext
from .google.client import GoogleAPI

EXPIRY = timedelta(hours=48)


class ApprovalError(Exception):
    pass


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _clip(text: str, n: int = 80) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------- creating cards

# What the owner may change when editing a card. Everything else (which email
# or event, the resolved recipient and thread) stays as the card stored it.
EDITABLE = {
    "gmail.send": {"to", "cc", "subject", "body"},
    "gmail.reply": {"body"},
    "calendar.respond": {"response"},
    "gmail.trash": set(),
    "calendar.delete": set(),
}


def prepare(action: str, args: dict, google_read: Callable[[], GoogleAPI]) -> dict:
    """Resolve what the action will really do. Returns the args to store; display
    details go under "_display" (the executor ignores them)."""
    if action == "gmail.send":
        return dict(args)
    if action in ("gmail.reply", "gmail.trash"):
        g = google_read()
        try:
            orig = g.get(f"/gmail/v1/users/me/messages/{args['message_id']}", format="metadata",
                         metadataHeaders=["From", "Reply-To", "Subject", "Message-ID", "References"])
        finally:
            g.close()
        h = {x["name"].lower(): x["value"] for x in orig["payload"]["headers"]}
        if action == "gmail.trash":
            return {**args, "_display": {"from": h.get("from", "?"), "subject": h.get("subject", "")}}
        to = first_address(h.get("reply-to")) or first_address(h.get("from"))
        if not to:
            raise ApprovalError("can't tell who to reply to")
        subject = h.get("subject", "")
        if not subject.lower().startswith("re:"):
            subject = "Re: " + subject
        refs = " ".join(p for p in [h.get("references", "").strip(), h.get("message-id", "").strip()] if p) or None
        return {**args, "to": [to], "subject": subject, "thread_id": orig.get("threadId"),
                "in_reply_to": h.get("message-id"), "references": refs}
    if action in ("calendar.respond", "calendar.delete"):
        g = google_read()
        try:
            ev = g.get(f"/calendar/v3/calendars/primary/events/{args['event_id']}")
        finally:
            g.close()
        start = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date") or "?"
        display = {"title": ev.get("summary") or "(no title)", "start": start,
                   "organizer": (ev.get("organizer") or {}).get("email", "?")}
        if action == "calendar.respond":
            return {**args, "_display": display}
        others = [a for a in ev.get("attendees") or [] if not a.get("self")]
        display["others"] = len(others)
        return {**args, "send_updates": "all" if others else "none", "_display": display}
    raise ApprovalError(f"{action} has no approval card")


def summarize(action: str, a: dict) -> str:
    """Built by the backend from the stored args only."""
    d = a.get("_display", {})
    if action == "gmail.send":
        return f'Send email to {", ".join(a["to"])}: "{_clip(a["subject"], 60)}" - "{_clip(a["body"])}"'
    if action == "gmail.reply":
        return f'Reply to {a["to"][0]} ("{_clip(a["subject"], 50)}"): "{_clip(a["body"])}"'
    if action == "gmail.trash":
        return f'Move email to trash: from {d.get("from", "?")}, "{_clip(d.get("subject", ""), 60)}"'
    if action == "calendar.respond":
        verb = {"accepted": "Accept", "declined": "Decline", "tentative": "Maybe"}[a["response"]]
        return f'{verb} "{_clip(d.get("title", "?"), 60)}" ({d.get("start", "?")}); {d.get("organizer", "?")} will be told'
    if action == "calendar.delete":
        n = d.get("others", 0)
        note = f"; {n} other attendee{'s' if n != 1 else ''} will be notified" if n else ""
        return f'Delete event "{_clip(d.get("title", "?"), 60)}" ({d.get("start", "?")}){note}'
    return action


def create_card(session: Session, decision: Decision, *, executor: Executor, google_read: Callable[[], GoogleAPI],
                thread_id: int | None, reason: str = "", actor: str = "brain") -> Approval:
    """actor: who asked for it. "brain" for the model's proposals, "owner" for a tap in the app (Loop Send)."""
    if decision.verdict != "approve":
        raise ApprovalError("only approve-verdicts become cards")
    stored = prepare(decision.action, decision.args, google_read)
    card = Approval(tool=decision.action, args_json=json.dumps(stored), summary=summarize(decision.action, stored), tier=decision.tier,
                    status="open", thread_id=thread_id, reason=(reason or "")[:2000])
    session.add(card)
    session.commit()
    session.refresh(card)
    audit(session, actor=actor, action=decision.action, tier=decision.tier, args=stored, outcome=f"card #{card.id} created")
    session.refresh(card)
    return card


# ---------------------------------------------------------------- deciding cards

def expire_old(session: Session) -> int:
    cutoff = datetime.now(timezone.utc) - EXPIRY
    n = 0
    for card in session.exec(select(Approval).where(Approval.status == "open")).all():
        if _aware(card.created_at) < cutoff:
            card.status = "expired"
            card.decided_at = utcnow()
            session.add(card)
            n += 1
    if n:
        session.commit()
    return n


def _open_card(session: Session, card_id: int) -> Approval:
    expire_old(session)
    card = session.get(Approval, card_id)
    if card is None:
        raise ApprovalError(f"no card #{card_id}")
    if card.status != "open":
        raise ApprovalError(f"card #{card_id} is {card.status}")
    return card


def _outbound_last_24h(session: Session) -> int:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    rows = session.exec(select(Approval).where(Approval.tier == 3, Approval.status == "done")).all()
    return sum(1 for r in rows if r.decided_at and _aware(r.decided_at) >= since)


def check_approvable(session: Session, tier: int, *, gate: Gate, confirm: bool = False) -> None:
    """Everything approve() checks before it runs a card. Loop Send asks first, so its card
    is never created just to be refused (fix P1.7)."""
    if killswitch.is_paused(session):     # fix P1.5: nothing runs while paused, approved by hand or not
        raise ApprovalError("Kairo is paused (kill switch): nothing is executed. Resume it under More, then approve.")
    if tier >= 4 and not confirm:
        raise ApprovalError("this one needs a second confirmation: confirm a second time")
    if tier == 3 and _outbound_last_24h(session) >= gate.outbound_per_day_max:
        raise ApprovalError(f"daily limit of {gate.outbound_per_day_max} outbound actions reached")


def withdraw(session: Session, card_id: int, why: str) -> Approval:
    """A card the owner created and approved in one tap that still couldn't run: closed as failed, never left open."""
    card = session.get(Approval, card_id)
    if card is not None and card.status == "open":
        card.status = "failed"
        card.decided_at = utcnow()
        card.result_json = json.dumps({"error": why[:1000]})
        session.add(card)
        session.commit()
        audit(session, actor="backend", action=card.tool, tier=card.tier, args=json.loads(card.args_json),
              outcome=f"card #{card.id} not run: {why[:300]}")
    return card


def approve(session: Session, card_id: int, *, executor: Executor, gate: Gate, confirm: bool = False,
            edited_args: dict | None = None) -> Approval:
    card = _open_card(session, card_id)
    check_approvable(session, card.tier, gate=gate, confirm=confirm)

    args = json.loads(card.args_json)
    if edited_args is not None:
        allowed = EDITABLE.get(card.tool, set())
        bad = sorted(set(edited_args) - allowed)
        if bad:
            raise ApprovalError(f"can't change {', '.join(bad)} on this card")
        candidate = {k: v for k, v in args.items() if k in _schema_fields(card.tool)}
        candidate.update(edited_args)
        ctx = GateContext(paused=killswitch.is_paused(session), origin="owner_chat", open_approvals=0, outbound_last_24h=0)
        d = gate.decide("mcp__kairo__propose_action", {"action": card.tool, "args": candidate}, ctx)
        if d.verdict != "approve":
            raise ApprovalError(f"the edit isn't allowed: {d.reason}")
        args.update({k: d.args[k] for k in edited_args})
        card.args_json = json.dumps(args)
        card.summary = summarize(card.tool, args)

    card.status = "approved"
    card.decided_at = utcnow()
    session.add(card)
    session.commit()
    audit(session, actor="owner", action=card.tool, tier=card.tier, args=args, outcome=f"card #{card.id} approved")

    try:
        result = executor.run(card.tool, args)
        card.status = "done"
        card.result_json = json.dumps(result, default=str)
        outcome = f"executed card #{card.id}"
        note = f"#{card.id} done: {card.summary}"
    except Exception as exc:  # report, never retry automatically
        card.status = "failed"
        card.result_json = json.dumps({"error": str(exc)[:1000]})
        outcome = f"failed card #{card.id}: {str(exc)[:300]}"
        note = f"#{card.id} failed: {card.summary}. Error: {str(exc)[:300]}"
    session.add(card)
    session.commit()
    audit(session, actor="backend", action=card.tool, tier=card.tier, args=args, outcome=outcome)
    if card.thread_id:
        session.add(ChatMessage(thread_id=card.thread_id, role="system", text=note))
        session.commit()
    session.refresh(card)
    return card


def reject(session: Session, card_id: int) -> Approval:
    card = _open_card(session, card_id)
    card.status = "rejected"
    card.decided_at = utcnow()
    session.add(card)
    session.commit()
    audit(session, actor="owner", action=card.tool, tier=card.tier, args=json.loads(card.args_json), outcome=f"card #{card.id} rejected")
    if card.thread_id:
        session.add(ChatMessage(thread_id=card.thread_id, role="system", text=f"#{card.id} rejected: {card.summary}"))
        session.commit()
    session.refresh(card)
    return card


def _schema_fields(action: str) -> set[str]:
    from .actions import SCHEMAS
    return set(SCHEMAS[action].model_fields)


def card_dict(card: Approval) -> dict:
    return {"id": card.id, "action": card.tool, "tier": card.tier, "summary": card.summary,
            "args": json.loads(card.args_json), "editable": sorted(EDITABLE.get(card.tool, set())), "status": card.status, "reason": card.reason,
            "created_at": card.created_at, "decided_at": card.decided_at,
            "expires_at": _aware(card.created_at) + EXPIRY, "confirm_twice": card.tier >= 4,
            "result": json.loads(card.result_json) if card.result_json else None}
