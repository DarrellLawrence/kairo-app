"""Tiny MCP server for the real-CLI hook tests: one tool, `touch`, that writes
the file named in PROBE_MARKER. If the marker exists, the tool really ran."""

import json
import os
import sys

for line in sys.stdin:
    msg = json.loads(line)
    mid = msg.get("id")
    if mid is None:
        continue
    method = msg.get("method")
    if method == "initialize":
        result = {"protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "probe", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "touch", "description": "Creates the test marker file. Call it when asked.",
                             "inputSchema": {"type": "object", "properties": {}}}]}
    elif method == "tools/call":
        with open(os.environ["PROBE_MARKER"], "w") as fh:
            fh.write("ran")
        result = {"content": [{"type": "text", "text": "touched"}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
    sys.stdout.flush()
