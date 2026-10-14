#!/usr/bin/env python3
"""Fake `claude` binary for tests. Mimics `claude -p --output-format stream-json`.

Behaviour is picked with FAKE_CLAUDE_MODE (read from KAIRO_HOOK_FAKE_MODE too,
because the adapter only passes allow-listed variables through):
  ok           echo the prompt back (default)
  usage_limit  rate_limit_event rejected + error result
  logged_out   error result asking to /login
  crash        exit 1 with a message on stderr, no result
  slow         sleep 30 s before answering (timeout tests)
  pause        1.5 s pause in the middle of the answer (hang-up tests)
  bad_flag     exit 1 like the real CLI does on a missing --verbose

FAKE_CLAUDE_NO_TOOLS=1: init lists no tools although MCP connected. FAKE_CLAUDE_SKIP_TOOLS=1: the
"model" ignores [[fake-tool]] directives (asks instead of acting).
FAKE_CLAUDE_PROBE=<report.json> (+ FAKE_CLAUDE_PROBE_PATHS=a;b): write who ran
the CLI, whether it is admin, and which of the paths it can read.

If FAKE_CLAUDE_RECORD (or KAIRO_HOOK_FAKE_RECORD) is set, argv, stdin, cwd and
the environment are written there as JSON (plus the pid and every turn's prompt).

--include-partial-messages: text also comes as stream_event deltas before the full assistant
message, like the real CLI (2.1.299). --input-format stream-json: one user message per stdin line,
one turn each, until stdin closes (a warm process serves several turns).

Tools, like the real CLI (used by the release tool test and the self-test): when the
prompt contains `[[fake-tool NAME {json args}]]`, the fake starts the MCP servers from
--mcp-config (stdio JSON-RPC), reports their status in system/init, runs the
PreToolUse hooks from <cwd>/.claude/settings.json before the call (exit 2 = blocked),
then calls mcp__<server>__NAME over MCP. The reply says TOOL-RESULT / TOOL-BLOCKED /
TOOL-UNAVAILABLE NAME: ... for each directive.
"""

import json
import os
import re
import subprocess
import sys
import time
import uuid

DIRECTIVE = re.compile(r"\[\[fake-tool (\w+) (\{.*?\})\]\]", re.S)


def env(name):
    return os.environ.get(name) or os.environ.get(name.replace("FAKE_CLAUDE_", "KAIRO_HOOK_FAKE_"))


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def write_probe(report_path, paths):
    """CI: which account runs the CLI, is it admin, what can it read?"""
    admin = None
    if os.name == "nt":
        # Ask Windows, not the environment: USERNAME may not be passed through.
        import ctypes
        buf = ctypes.create_unicode_buffer(257)
        size = ctypes.c_uint32(257)
        ctypes.windll.advapi32.GetUserNameW(buf, ctypes.byref(size))
        user = buf.value
        admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    else:
        import pwd
        user = pwd.getpwuid(os.getuid()).pw_name
        admin = os.geteuid() == 0
    readable = {}
    for path in filter(None, paths.split(";")):
        try:
            with open(path, "rb") as fh:
                fh.read(1)
            readable[path] = True
        except OSError:
            readable[path] = False
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump({"user": user, "admin": admin, "readable": readable}, fh)


class MCPClient:
    """Minimal stdio MCP client, like the real CLI's: newline-delimited JSON-RPC."""

    def __init__(self, spec):
        env = {**os.environ, **(spec.get("env") or {})}
        self.proc = subprocess.Popen([spec["command"], *spec.get("args", [])], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True, encoding="utf-8")
        self.next_id = 0

    def request(self, method, params=None):
        self.next_id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("MCP server closed: " + self.proc.stderr.read()[:300])
        msg = json.loads(line)
        if "error" in msg:
            raise RuntimeError(msg["error"].get("message"))
        return msg["result"]

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def start_mcp(args):
    """Start every server in --mcp-config. Returns ({name: client}, [{name, status}])."""
    if "--mcp-config" not in args:
        return {}, []
    spec = json.load(open(args[args.index("--mcp-config") + 1], encoding="utf-8"))
    clients, status = {}, []
    for name, srv in (spec.get("mcpServers") or {}).items():
        try:
            c = MCPClient(srv)
            c.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fake-claude"}})
            c.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            c.proc.stdin.flush()
            clients[name] = c
            status.append({"name": name, "status": "connected"})
        except Exception:
            status.append({"name": name, "status": "failed"})
    return clients, status


def list_tools(clients):
    names = []
    for server, c in clients.items():
        try:
            names += [f"mcp__{server}__{t['name']}" for t in c.request("tools/list").get("tools", [])]
        except Exception:
            pass
    return names


def run_hooks(tool_name, tool_input):
    """PreToolUse hooks from <cwd>/.claude/settings.json, exec form, like the real CLI.
    Returns (allowed, message). A hook that can't start or times out blocks (onFailure: block)."""
    path = os.path.join(os.getcwd(), ".claude", "settings.json")
    if not os.path.exists(path):
        return True, ""
    settings = json.load(open(path, encoding="utf-8"))
    for entry in (settings.get("hooks") or {}).get("PreToolUse", []):
        if entry.get("matcher") not in ("*", tool_name):
            continue
        for hook in entry.get("hooks", []):
            payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": tool_input})
            try:
                r = subprocess.run([hook["command"], *hook.get("args", [])], input=payload, capture_output=True,
                                   text=True, timeout=hook.get("timeout", 60), env=dict(os.environ))
            except (OSError, subprocess.TimeoutExpired) as exc:
                return False, f"hook failed to run: {exc}"
            if r.returncode == 2:
                return False, (r.stderr or "").strip()[:300]
            if r.returncode != 0:
                return False, f"hook exit {r.returncode}: {(r.stderr or '').strip()[:200]}"
    return True, ""


def tool_calls(prompt, args, clients, sid):
    """Run every [[fake-tool NAME {json}]] directive. Returns reply lines."""
    allowed = set(args[args.index("--allowedTools") + 1].split(",")) if "--allowedTools" in args else set()
    lines = []
    for name, raw in DIRECTIVE.findall(prompt):
        tool_input = json.loads(raw)
        server = next(iter(clients), None)
        full = f"mcp__{server}__{name}" if server else f"mcp__kairo__{name}"
        if server is None or full not in allowed:
            lines.append(f"TOOL-UNAVAILABLE {name}")
            continue
        out({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:12], "name": full, "input": tool_input}]}, "session_id": sid})
        ok, why = run_hooks(full, tool_input)
        if not ok:
            lines.append(f"TOOL-BLOCKED {name}: {why}")
            continue
        result = clients[server].request("tools/call", {"name": name, "arguments": tool_input})
        text = " ".join(c.get("text", "") for c in result.get("content", []))
        lines.append(f"TOOL-{'ERROR' if result.get('isError') else 'RESULT'} {name}: {text}")
    return lines


def say(text, sid, partial, pause=0.0):
    """One assistant text message; with --include-partial-messages first as deltas (two halves)."""
    if partial:
        out({"type": "stream_event", "event": {"type": "message_start"}, "session_id": sid})
        half = len(text) // 2
        for i, chunk in enumerate((text[:half], text[half:])):
            if chunk:
                out({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                       "delta": {"type": "text_delta", "text": chunk}}, "session_id": sid})
            if i == 0 and pause:
                time.sleep(pause)
        out({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}, "session_id": sid})
        return
    half = len(text) // 2
    out({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text[:half]}]}, "session_id": sid})
    if pause:
        time.sleep(pause)
    out({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text[half:]}]}, "session_id": sid})


def turn(args, prompt, sid, mode, partial):
    """One turn: init, the answer, the result. Returns the exit code for a one-turn process."""
    clients, mcp_status = start_mcp(args) if DIRECTIVE.search(prompt) else ({}, [])
    # Like the real CLI (2.1.296): init lists every tool of every connected MCP server.
    listed = [] if env("FAKE_CLAUDE_NO_TOOLS") else list_tools(clients)
    out({"type": "system", "subtype": "init", "session_id": sid, "tools": listed, "mcp_servers": mcp_status})
    if clients or mcp_status:
        try:
            lines = ["I'd rather not do that."] if env("FAKE_CLAUDE_SKIP_TOOLS") else tool_calls(prompt, args, clients, sid)
        finally:
            for c in clients.values():
                c.close()
        reply = "\n".join(lines)
        say(reply, sid, partial)
        out({"type": "result", "subtype": "success", "is_error": False, "result": reply, "session_id": sid})
        return 0

    if mode == "slow":
        time.sleep(30)
    if mode == "usage_limit":
        out({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour"}, "session_id": sid})
        out({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
             "result": "Claude usage limit reached. Your limit will reset at 5pm.", "session_id": sid})
        return 1
    if mode == "logged_out":
        out({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 401,
             "result": "Invalid API key · Please run /login", "session_id": sid})
        return 1

    reply = "echo: " + prompt
    out({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}, "session_id": sid})
    say(reply, sid, partial, pause=1.5 if mode == "pause" else 0.0)
    out({"type": "result", "subtype": "success", "is_error": False, "result": reply, "session_id": sid})
    return 0


def user_text(line):
    msg = json.loads(line)
    parts = msg["message"]["content"]
    images = sum(1 for p in parts if p.get("type") == "image")
    text = " ".join(p.get("text", "") for p in parts if p.get("type") == "text")
    return f"[{images} image(s)] {text}" if images else text


def main():
    args = sys.argv[1:]
    if "--version" in args:
        print("2.1.296 (Claude Code)")
        return 0
    stream_in = "--input-format" in args and args[args.index("--input-format") + 1] == "stream-json"
    partial = "--include-partial-messages" in args
    record = env("FAKE_CLAUDE_RECORD")
    system = ""
    if "--system-prompt-file" in args:
        with open(args[args.index("--system-prompt-file") + 1], encoding="utf-8") as fh:
            system = fh.read()   # read once at start, like the real CLI
    turns = []

    def write_record(prompt):
        turns.append(prompt)
        if record:
            with open(record, "w", encoding="utf-8") as fh:
                json.dump({"argv": args, "stdin": prompt, "cwd": os.getcwd(), "env": dict(os.environ), "system": system,
                           "pid": os.getpid(), "turns": turns}, fh)

    probe = env("FAKE_CLAUDE_PROBE")
    if probe:
        write_probe(probe, env("FAKE_CLAUDE_PROBE_PATHS") or "")

    mode = env("FAKE_CLAUDE_MODE") or "ok"
    if "-p" in args and "stream-json" in args and "--verbose" not in args:
        mode = "bad_flag"
    if mode == "bad_flag":
        sys.stderr.write("Error: When using --print, --output-format=stream-json requires --verbose\n")
        return 1
    if mode == "crash":
        sys.stdin.read()
        sys.stderr.write("boom: fake crash\n")
        return 1

    sid = args[args.index("--resume") + 1] if "--resume" in args else str(uuid.uuid4())
    if not stream_in:
        prompt = sys.stdin.read()
        write_record(prompt)
        return turn(args, prompt, sid, mode, partial)
    code = 0
    for line in sys.stdin:   # like the real CLI: one user message per line, one turn each
        if not line.strip():
            continue
        prompt = user_text(line)
        write_record(prompt)
        code = turn(args, prompt, sid, mode, partial)
    return code


if __name__ == "__main__":
    sys.exit(main())
