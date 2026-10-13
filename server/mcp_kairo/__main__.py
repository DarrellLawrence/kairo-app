"""mcp_kairo: Kairo's only MCP server (KICKOFF 7).

A thin stdio bridge with no logic and no credentials: tools/list and
tools/call are forwarded to the backend over 127.0.0.1, which runs the gate
and does the work. Started by the Claude CLI from the workspace .mcp.json.

Environment: KAIRO_TOOL_URL (http://127.0.0.1:<port>/internal/tools),
KAIRO_TOOL_TOKEN (per-start secret). Standard library only: starts fast.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

SERVER_INFO = {"name": "kairo", "version": "1"}


def backend(path: str, payload: dict | None = None) -> dict:
    url = os.environ["KAIRO_TOOL_URL"].rstrip("/") + path
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="GET" if data is None else "POST",
                                 headers={"Authorization": "Bearer " + os.environ["KAIRO_TOOL_TOKEN"],
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    mid = msg.get("id")
    if mid is None:
        return None  # notification
    try:
        if method == "initialize":
            version = (msg.get("params") or {}).get("protocolVersion", "2025-06-18")
            result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": backend("/list")["tools"]}
        elif method == "tools/call":
            params = msg.get("params") or {}
            out = backend("/call", {"name": params.get("name"), "arguments": params.get("arguments") or {}})
            result = {"content": [{"type": "text", "text": out["text"]}], "isError": bool(out.get("is_error"))}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
    except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
        if method == "tools/call":
            result = {"content": [{"type": "text", "text": f"Error: Kairo backend unavailable ({exc})"}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        reply = handle(msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
