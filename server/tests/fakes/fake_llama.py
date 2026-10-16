#!/usr/bin/env python3
"""Fake llama-server for tests: the same flags and the same OpenAI-style streaming as llama.cpp's
server (checked against the real one built from the Oct 2026 source: content deltas, tool_calls
deltas {index, id, type, function{name, arguments}}, a last chunk with usage and timings).

The "model": a question the Today card in the system prompt answers (weather, the next train, what's
next) -> "From your card: <that line>" (FAKE_LLAMA_NO_CARD=1: it ignores the card). A user message
about the owner's data (calendar, remind, mail, list, test, Kalender, erinner, Liste, Prüfung, ...)
with tools offered -> one ask_kairo call with the message as request. A tool result or a note that an
answer is here -> "Okay. <the answer>". Anything else -> "Sure, happy to help with that." in pieces.

FAKE_LLAMA_LOAD_S: seconds of 503 on /health before the model is "loaded".
FAKE_LLAMA_TOKEN_MS: delay between pieces (default 5). FAKE_LLAMA_FIRST_MS: before the first piece.
FAKE_LLAMA_RECORD: every request body is appended there as one JSON line.
FAKE_LLAMA_NO_TOOLS=1: never calls the tool (a model that ignores tools). FAKE_LLAMA_ALWAYS_TOOL=1: always calls it.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARGS = sys.argv[1:]
PORT = int(ARGS[ARGS.index("--port") + 1]) if "--port" in ARGS else 8091
MODEL = ARGS[ARGS.index("-m") + 1] if "-m" in ARGS else "fake.gguf"
STARTED = time.monotonic()
LOAD_S = float(os.environ.get("FAKE_LLAMA_LOAD_S", "0"))
TOKEN_S = float(os.environ.get("FAKE_LLAMA_TOKEN_MS", "5")) / 1000
FIRST_S = float(os.environ.get("FAKE_LLAMA_FIRST_MS", "0")) / 1000
CARD = [(re.compile(r"weather|wetter|\bwarm|\brain\b|regn|temperat|\bcold|kalt", re.I), "## Weather"),
        (re.compile(r"\btrain\b|\bbus\b|\bzug\b|connection|verbindung|platform|gleis|\bmiss\b|verpass", re.I), "## Next connection"),
        (re.compile(r"buy|kauf|overdue|überfällig|lock|schloss|bill|rechnung", re.I), "## Today's tasks"),
        (re.compile(r"lea\b|promise|versproch|wollte", re.I), "## Open loops"),
        (re.compile(r"next|nächst|training|school|schule|friday|freitag|cinema|kino|tomorrow|morgen", re.I), "## Next events")]
DATA = re.compile(r"calendar|remind|mail|list|test|exam|note|task|send|move|kalender|erinner|liste|prüfung|notiz|aufgabe|schick", re.I)


def from_card(system: str, text: str) -> str | None:
    """The whole card section the question is about (the facts are somewhere in it)."""
    if os.environ.get("FAKE_LLAMA_NO_CARD") or "# Today card" not in system:
        return None
    text = re.sub(r"^\s*\[\d{1,2}:\d{2}\]\s*", "", text.rsplit("</kairo-note>", 1)[-1])
    for rx, header in CARD:
        if rx.search(text) and header in system:
            lines = []
            for ln in system.split(header, 1)[1].splitlines()[1:]:
                ln = ln.strip()
                if ln.startswith("## ") or ln.startswith("# "):
                    break
                if ln and not ln.startswith("<<<") and not ln.endswith(":"):
                    lines.append(ln.lstrip("- "))
            return "; ".join(lines) or None
    return None


def chunk(delta=None, finish=None, extra=None):
    d = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}], "object": "chat.completion.chunk",
         "model": MODEL}
    if extra:
        d.update(extra)
    return d


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            if time.monotonic() - STARTED < LOAD_S:
                self._json(503, {"error": {"message": "Loading model"}})
            else:
                self._json(200, {"status": "ok"})
        elif self.path == "/props":
            self._json(200, {"model_path": MODEL, "total_slots": 1})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if os.environ.get("FAKE_LLAMA_RECORD"):
            with open(os.environ["FAKE_LLAMA_RECORD"], "a", encoding="utf-8") as f:
                f.write(json.dumps(body) + "\n")
        msgs = body.get("messages") or []
        last = msgs[-1] if msgs else {}
        text = str(last.get("content") or "")
        tools = body.get("tools") and not os.environ.get("FAKE_LLAMA_NO_TOOLS")
        pieces, call = [], None
        system = str(msgs[0].get("content") or "") if msgs and msgs[0].get("role") == "system" else ""
        card = from_card(system, text) if last.get("role") == "user" else None
        here = re.search(r"is here: (.*?)\nSay it now", text, re.S)
        if here:
            pieces = ["Okay. "] + [w + " " for w in here.group(1).split()]
        elif card and not (tools and DATA.search(text.rsplit("</kairo-note>", 1)[-1])):
            pieces = ["From ", "your ", "card: "] + [w + " " for w in card.split()]
        elif last.get("role") == "tool":
            try:
                said = json.loads(text).get("answer", text)
            except ValueError:
                said = text
            pieces = ["Okay. "] + [w + " " for w in str(said).split()]
        elif tools and (os.environ.get("FAKE_LLAMA_ALWAYS_TOOL") or DATA.search(text)):
            call = json.dumps({"request": text}, ensure_ascii=False)
        else:
            pieces = ["Sure, ", "happy ", "to ", "help ", "with ", "that."]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            time.sleep(FIRST_S)
            self._send(chunk({"role": "assistant", "content": None}))
            if call is not None:
                self._send(chunk({"tool_calls": [{"index": 0, "id": "call_fake1", "type": "function",
                                                  "function": {"name": "ask_kairo", "arguments": ""}}]}))
                for i in range(0, len(call), max(1, len(call) // 3)):
                    time.sleep(TOKEN_S)
                    self._send(chunk({"tool_calls": [{"index": 0, "function": {"arguments": call[i:i + max(1, len(call) // 3)]}}]}))
                finish, n = "tool_calls", 20
            else:
                for p in pieces:
                    time.sleep(TOKEN_S)
                    self._send(chunk({"content": p}))
                finish, n = "stop", len(pieces)
            self._send(chunk(finish=finish))
            self._send({"choices": [], "usage": {"completion_tokens": n, "prompt_tokens": 100},
                        "timings": {"cache_n": 80, "prompt_n": 20, "prompt_ms": 5.0, "predicted_n": n,
                                    "predicted_per_second": 120.0}})
            self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):   # the client hung up (talked over)
            pass

    def _send(self, obj):
        self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode())
        self.wfile.flush()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
