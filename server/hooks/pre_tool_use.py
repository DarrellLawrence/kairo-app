"""Claude Code PreToolUse hook -> Kairo's gate. FAILS CLOSED.

Registered in the CLI workspace's .claude/settings.json (exec form: this
Python + this file, no shell; onFailure: block; short timeout).

Claude Code only blocks on exit code 2. Exit 1, a crash, a timeout or a hook
that can't start would let the tool run, so: every path that is not a clear
"no objection" exits 2, including any exception, a missing setting, a broken
permissions.yaml or an unreachable database.

On "no objection" it prints nothing and exits 0: the hook can only object,
never grant. The CLI's own allow-list still has to allow the tool.

Environment (set by the backend for each CLI run):
  KAIRO_HOOK_CONFIG_DIR  folder with permissions.yaml
  KAIRO_HOOK_DB_URL      Kairo's database (kill switch, open cards, limits, and for an owner_chat run the speaker
                         label of the voice turn it is running: gate_context.read_context(model_turn=True))
  KAIRO_HOOK_ORIGIN      owner_chat | job

The speaker label (G13) never comes from the tool input or the environment: the backend wrote it to the database
before Claude ran, and nothing the model can call writes there. A label that can't be read blocks like any other error.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BLOCK = 2


def main() -> int:
    try:
        data = json.loads(sys.stdin.read())
        if not isinstance(data, dict) or data.get("hook_event_name") != "PreToolUse":
            raise ValueError("not a PreToolUse event")
        tool_name = data["tool_name"]
        tool_input = data.get("tool_input", {})
        config_dir = os.environ["KAIRO_HOOK_CONFIG_DIR"]
        db_url = os.environ["KAIRO_HOOK_DB_URL"]
        origin = os.environ["KAIRO_HOOK_ORIGIN"]

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from app.db import make_engine
        from app.gate import load_gate
        from app.gate_context import read_context

        gate = load_gate(Path(config_dir))
        engine = make_engine(db_url)
        try:
            ctx = read_context(engine, origin, model_turn=True)
        finally:
            engine.dispose()
        decision = gate.decide(tool_name, tool_input, ctx)
        if decision.allowed:
            return 0
        print(f"Kairo gate: {decision.reason}", file=sys.stderr)
        return BLOCK
    except BaseException as exc:  # noqa: BLE001 - everything, including SystemExit, blocks
        try:
            print(f"Kairo gate failed closed: {type(exc).__name__}: {exc}", file=sys.stderr)
        except BaseException:  # noqa: BLE001
            pass
        return BLOCK


if __name__ == "__main__":
    code = BLOCK
    try:
        code = main()
    finally:
        sys.stderr.flush()
        os._exit(code)  # no interpreter-shutdown surprises can change the exit code
