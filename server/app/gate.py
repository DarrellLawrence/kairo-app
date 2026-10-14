"""The permission gate (KICKOFF 4, 8). Pure code: no model, no I/O in decide().

Every tool call the model makes is decided here twice: by the PreToolUse hook
(before the CLI runs the tool) and by the backend when the call arrives
through mcp_kairo. Anything not explicitly allowed is denied.

Verdicts:
  allow    tier 0-1 direct tool: run it
  execute  tier 2 proposal: the backend runs it now (logged, in the digest)
  approve  tier 3-4 proposal: store it as an approval card, run only after the owner approves
  deny     anything else
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ValidationError

from .actions import DIRECT_TOOLS, MIN_TIER, NOTIFYING_FIELDS, SCHEMAS, ProposeAction

TOOL_PREFIX = "mcp__kairo__"
PROPOSE_TOOL = "propose_action"

TIER_NAMES = {"0_read": 0, "1_internal": 1, "2_reversible": 2, "3_outbound": 3, "4_destructive": 4, "5_money": 5}
REQUIRED_MODE = {0: "auto", 1: "auto", 2: "auto", 3: "approve", 4: "approve", 5: "never"}

Verdict = Literal["allow", "execute", "approve", "deny"]


class GateConfigError(Exception):
    """permissions.yaml is missing, malformed, or would weaken the rules."""


@dataclass(frozen=True)
class GateContext:
    paused: bool
    origin: Literal["owner_chat", "job", "voice_note"]
    open_approvals: int
    outbound_last_24h: int


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str
    action: str | None = None
    tier: int | None = None
    args: dict[str, Any] = field(default_factory=dict)
    confirm_twice: bool = False

    @property
    def allowed(self) -> bool:
        return self.verdict != "deny"


def _deny(reason: str, action: str | None = None, tier: int | None = None) -> Decision:
    return Decision("deny", reason, action, tier)


@dataclass(frozen=True)
class Gate:
    actions: dict[str, int]            # action -> tier
    confirm_twice_tiers: frozenset[int]
    hold_allowed_fields: frozenset[str]
    approvals_open_max: int
    outbound_per_day_max: int

    def actions_without_schema(self) -> list[str]:
        return sorted(a for a in self.actions if a not in SCHEMAS)

    def decide(self, tool_name: str, tool_input: Any, ctx: GateContext) -> Decision:
        if ctx.paused:
            return _deny("Kairo is paused (kill switch). Nothing runs until the owner resumes it.")
        if not isinstance(tool_name, str) or not tool_name.startswith(TOOL_PREFIX):
            return _deny(f"{tool_name!r} is not a Kairo tool. Only Kairo's own tools exist.")
        if not isinstance(tool_input, dict):
            return _deny("tool input must be an object")
        name = tool_name[len(TOOL_PREFIX):]
        if name == PROPOSE_TOOL:
            return self._decide_proposal(tool_input, ctx)
        action = DIRECT_TOOLS.get(name)
        if action is None or action not in self.actions:
            return _deny(f"unknown tool {name!r} (default deny)")
        tier = self.actions[action]
        if tier > 1:
            return _deny(f"{action} is tier {tier}; it can only be proposed", action, tier)
        args = self._validate(action, tool_input)
        if isinstance(args, str):
            return _deny(args, action, tier)
        return Decision("allow", f"{action}: tier {tier}, allowed", action, tier, args)

    def _decide_proposal(self, tool_input: dict, ctx: GateContext) -> Decision:
        try:
            proposal = ProposeAction.model_validate(tool_input)
        except ValidationError as exc:
            return _deny(f"bad proposal: {_short(exc)}")
        action = proposal.action
        tier = self.actions.get(action)
        if tier is None:
            return _deny(f"unknown action {action!r} (default deny)", action)
        if tier == 5:
            return _deny("Money actions do not exist in Kairo. It can't pay, transfer or buy anything.", action, 5)
        if tier < 2:
            return _deny(f"{action} is tier {tier}; call its tool directly instead of proposing it", action, tier)
        if tier == 2:
            notifying = sorted(set(proposal.args) & NOTIFYING_FIELDS)
            if notifying:
                return _deny(f"{action} with {', '.join(notifying)} would notify other people; not allowed", action, tier)
            if action == "calendar.create_hold":
                extra = sorted(set(proposal.args) - self.hold_allowed_fields)
                if extra:
                    return _deny(f"calendar.create_hold does not allow: {', '.join(extra)}", action, tier)
        args = self._validate(action, proposal.args)
        if isinstance(args, str):
            return _deny(args, action, tier)
        if tier == 2:
            return Decision("execute", f"{action}: tier 2, runs now and goes in the digest", action, tier, args)
        if ctx.origin != "owner_chat":
            return _deny(f"{action} needs the owner: only the app can create approval cards (not jobs or voice notes)", action, tier)
        if ctx.open_approvals >= self.approvals_open_max:
            return _deny(f"{self.approvals_open_max} approval cards are already open; decide those first", action, tier)
        if tier == 3 and ctx.outbound_last_24h >= self.outbound_per_day_max:
            return _deny(f"daily limit of {self.outbound_per_day_max} outbound actions reached", action, tier)
        return Decision("approve", f"{action}: tier {tier}, needs approval", action, tier, args,
                        confirm_twice=tier in self.confirm_twice_tiers)

    @staticmethod
    def _validate(action: str, args: dict) -> dict | str:
        schema = SCHEMAS.get(action)
        if schema is None:
            return f"{action} has no argument schema (default deny)"
        try:
            return schema.model_validate(args).model_dump(mode="json")
        except ValidationError as exc:
            return f"bad arguments for {action}: {_short(exc)}"


def _short(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:3]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "input"
        parts.append(f"{loc}: {err.get('msg')}")
    return "; ".join(parts)


class _Tier(BaseModel):
    mode: str
    digest: bool = False
    confirm_twice: bool = False


def load_gate(config_dir: Path) -> Gate:
    """Load permissions.yaml. Refuses (GateConfigError) anything that would weaken the gate."""
    path = Path(config_dir) / "permissions.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GateConfigError(f"{path} is missing") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise GateConfigError(f"{path} can't be read: {exc}") from exc
    if not isinstance(raw, dict):
        raise GateConfigError("permissions.yaml must be a mapping")
    if raw.get("default") != "deny":
        raise GateConfigError("permissions.yaml: default must be deny")

    try:
        tiers = {name: _Tier.model_validate(v) for name, v in (raw.get("tiers") or {}).items()}
    except ValidationError as exc:
        raise GateConfigError(f"permissions.yaml tiers: {exc}") from exc
    if set(tiers) != set(TIER_NAMES):
        raise GateConfigError(f"permissions.yaml must define exactly the tiers {sorted(TIER_NAMES)}")
    for name, tier in tiers.items():
        number = TIER_NAMES[name]
        if tier.mode != REQUIRED_MODE[number]:
            raise GateConfigError(f"tier {name} must have mode {REQUIRED_MODE[number]!r}, not {tier.mode!r}")
    if not tiers["4_destructive"].confirm_twice:
        raise GateConfigError("tier 4_destructive must keep confirm_twice: true")

    actions: dict[str, int] = {}
    for action, tier_name in (raw.get("actions") or {}).items():
        if tier_name not in TIER_NAMES:
            raise GateConfigError(f"action {action}: unknown tier {tier_name!r}")
        number = TIER_NAMES[tier_name]
        if number == 5:
            raise GateConfigError(f"action {action}: tier 5 (money) has no actions, ever")
        if action not in MIN_TIER:
            raise GateConfigError(f"action {action}: Kairo has no such action (add a schema and a test first)")
        if number < MIN_TIER[action]:
            raise GateConfigError(f"action {action}: tier {number} is looser than its minimum {MIN_TIER[action]}")
        actions[action] = number

    hold_rule = ((raw.get("rules") or {}).get("calendar.create_hold") or {})
    allowed = frozenset(hold_rule.get("allow_only_fields") or [])
    if allowed & NOTIFYING_FIELDS:
        raise GateConfigError(f"calendar.create_hold can't allow {sorted(allowed & NOTIFYING_FIELDS)}: they notify people")
    unknown = allowed - set(SCHEMAS["calendar.create_hold"].model_fields)
    if unknown:
        raise GateConfigError(f"calendar.create_hold allow_only_fields has unknown fields {sorted(unknown)}")

    limits = raw.get("limits") or {}
    try:
        open_max = int(limits.get("approvals_open_max", 10))
        out_max = int(limits.get("outbound_per_day_max", 20))
    except (TypeError, ValueError) as exc:
        raise GateConfigError(f"permissions.yaml limits: {exc}") from exc

    return Gate(
        actions=actions,
        confirm_twice_tiers=frozenset(TIER_NAMES[n] for n, t in tiers.items() if t.confirm_twice),
        hold_allowed_fields=allowed,
        approvals_open_max=open_max,
        outbound_per_day_max=out_max,
    )
