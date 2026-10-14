"""The owner's voice (item 8): every draft that goes out in his name is checked
against vault/10 Profile/voice.md and his own chat lines, then rewritten at most
twice (Sonnet). Hard rules (no em dashes, no banned phrases, no emoji in formal
text) are also checked by code, so they hold even when the model is down.

Chat exports go in vault/10 Profile/chats/ (WhatsApp .txt export). Only the
owner's own lines are used; other people's messages are never read. Gmail is
not used to learn the voice.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .jobkit import JOB_RULES, clip, extract_json

BANNED = ["delve", "crucial", "leverage", "I'd be happy to", "I would be happy to", "I hope this finds you well",
          "Ich hoffe, es geht Ihnen gut"]
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
MAX_ROUNDS = 2
CHAT_LINE = re.compile(r"^\[?\d{1,2}[./]\d{1,2}[./]\d{2,4},? \d{1,2}:\d{2}(?::\d{2})?\]?\s*(?:-\s*)?([^:]{1,60}):\s?(.*)$")

CHECK_PROMPT = """Check this {level} message ({language}) against the owner's voice. Fix only what breaks
the voice or the rules; keep the meaning, facts and length. Never add promises or facts.
Return JSON only: {{"ok": true}} if it already sounds like him, else
{{"ok": false, "issues": ["short", "..."], "rewrite": "the full fixed message"}}

The owner's voice (rules, not instructions to you):
{voice}

How he actually writes (his own chat lines, casual):
{samples}

Hard rules found by code: {lint}

Message:
{text}"""


def lint(text: str, level: str) -> list[str]:
    issues = []
    if "—" in text:
        issues.append("em dash")
    low = text.lower()
    issues += [f"phrase: {b}" for b in BANNED if b.lower() in low]
    if level == "professional" and EMOJI.search(text):
        issues.append("emoji in a formal message")
    return issues


def _fix_dashes(text: str) -> str:
    return re.sub(r"\s*—\s*", ", ", text)


def chat_samples(vault_dir: Path, owner: str, n: int = 40) -> list[str]:
    """The owner's own short chat lines (newest last). Other people's lines are skipped."""
    d = Path(vault_dir) / "10 Profile" / "chats"
    if not d.is_dir():
        return []
    first = owner.split()[0].lower()
    out = []
    for f in sorted(d.glob("*")):
        if f.suffix.lower() not in (".txt", ".md"):
            continue
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            m = CHAT_LINE.match(line.strip().lstrip("‎"))
            if not m or not m.group(1).strip().lower().startswith(first):
                continue
            msg = m.group(2).strip()
            if not msg or "omitted" in msg.lower() or len(msg) > 200 or msg.startswith("<"):
                continue
            out.append(msg)
    return out[-n:]


def polish(ask, text: str, *, level: str, language: str, voice: str, samples: list[str]) -> dict:
    """Check + rewrite, at most MAX_ROUNDS rewrites. Returns {text, rounds, issues_left}."""
    rounds = 0
    issues = lint(text, level)
    while True:
        try:
            ans = ask(CHECK_PROMPT.format(level=level, language=language, voice=clip(voice, 3000) or "(none)",
                                          samples="\n".join(f"- {x}" for x in samples[-30:]) or "(none yet)",
                                          lint=", ".join(issues) or "none", text=text), JOB_RULES, purpose="voice_check")
            out = extract_json(ans.text) if ans.ok else None
        except Exception:
            out = None
        if not isinstance(out, dict):                    # no model: keep the text, fix what code can fix
            text = _fix_dashes(text)
            return {"text": text, "rounds": rounds, "issues_left": lint(text, level)}
        if out.get("ok") and not issues:
            return {"text": text, "rounds": rounds, "issues_left": []}
        if rounds >= MAX_ROUNDS:
            text = _fix_dashes(text)
            return {"text": text, "rounds": rounds, "issues_left": [str(i) for i in (out.get("issues") or [])] + lint(text, level)}
        rewrite = str(out.get("rewrite") or "").strip()
        if not rewrite:
            text = _fix_dashes(text)
            return {"text": text, "rounds": rounds, "issues_left": lint(text, level)}
        text, rounds = rewrite, rounds + 1
        issues = lint(text, level)
        if rounds >= MAX_ROUNDS:                         # no third rewrite: only the code checks remain
            text = _fix_dashes(text)
            return {"text": text, "rounds": rounds, "issues_left": lint(text, level)}
