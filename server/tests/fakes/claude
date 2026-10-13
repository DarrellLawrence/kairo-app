#!/usr/bin/env python3
"""Fake `claude` binary for tests. Mimics `claude -p --output-format stream-json`.

Behaviour is picked with FAKE_CLAUDE_MODE (read from KAIRO_HOOK_FAKE_MODE too,
because the adapter only passes allow-listed variables through):
  ok           echo the prompt back (default)
  usage_limit  rate_limit_event rejected + error result
  logged_out   error result asking to /login
  crash        exit 1 with a message on stderr, no result
  slow         sleep 30 s before answering (timeout tests)
  bad_flag     exit 1 like the real CLI does on a missing --verbose

If FAKE_CLAUDE_RECORD (or KAIRO_HOOK_FAKE_RECORD) is set, argv, stdin, cwd and
the environment are written there as JSON.
"""

import json
import os
import sys
import time
import uuid


def env(name):
    return os.environ.get(name) or os.environ.get(name.replace("FAKE_CLAUDE_", "KAIRO_HOOK_FAKE_"))


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    args = sys.argv[1:]
    if "--version" in args:
        print("2.1.296 (Claude Code)")
        return 0
    prompt = sys.stdin.read()
    record = env("FAKE_CLAUDE_RECORD")
    if record:
        with open(record, "w", encoding="utf-8") as fh:
            json.dump({"argv": args, "stdin": prompt, "cwd": os.getcwd(), "env": dict(os.environ)}, fh)

    mode = env("FAKE_CLAUDE_MODE") or "ok"
    if "-p" in args and "stream-json" in args and "--verbose" not in args:
        mode = "bad_flag"
    if mode == "bad_flag":
        sys.stderr.write("Error: When using --print, --output-format=stream-json requires --verbose\n")
        return 1
    if mode == "crash":
        sys.stderr.write("boom: fake crash\n")
        return 1

    sid = args[args.index("--resume") + 1] if "--resume" in args else str(uuid.uuid4())
    out({"type": "system", "subtype": "init", "session_id": sid, "tools": [], "mcp_servers": []})

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
    half = len(reply) // 2
    out({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": reply[:half]}]}, "session_id": sid})
    out({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}, "session_id": sid})
    out({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": reply[half:]}]}, "session_id": sid})
    out({"type": "result", "subtype": "success", "is_error": False, "result": reply, "session_id": sid})
    return 0


if __name__ == "__main__":
    sys.exit(main())
