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

FAKE_CLAUDE_PROBE=<report.json> (+ FAKE_CLAUDE_PROBE_PATHS=a;b): write who ran
the CLI, whether it is admin, and which of the paths it can read.

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


def main():
    args = sys.argv[1:]
    if "--version" in args:
        print("2.1.296 (Claude Code)")
        return 0
    prompt = sys.stdin.read()
    if "--input-format" in args and args[args.index("--input-format") + 1] == "stream-json":
        msg = json.loads(prompt.splitlines()[0])   # like the real CLI: one user message per line
        parts = msg["message"]["content"]
        images = sum(1 for p in parts if p.get("type") == "image")
        prompt = f"[{images} image(s)] " + " ".join(p.get("text", "") for p in parts if p.get("type") == "text")
    record = env("FAKE_CLAUDE_RECORD")
    if record:
        with open(record, "w", encoding="utf-8") as fh:
            json.dump({"argv": args, "stdin": prompt, "cwd": os.getcwd(), "env": dict(os.environ)}, fh)

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
    if mode == "pause":
        time.sleep(1.5)
    out({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": reply[half:]}]}, "session_id": sid})
    out({"type": "result", "subtype": "success", "is_error": False, "result": reply, "session_id": sid})
    return 0


if __name__ == "__main__":
    sys.exit(main())
