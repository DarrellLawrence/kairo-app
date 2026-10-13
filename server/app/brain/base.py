"""Brain interface (KICKOFF section 7b). The model is only reached through here."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator, Protocol


class BrainStatus(str, Enum):
    ok = "ok"
    usage_limited = "usage_limited"
    logged_out = "logged_out"
    unavailable = "unavailable"  # binary missing / crashed; not in the spec's list, needed for the banner


class EventKind(str, Enum):
    session = "session"          # brain session id is known
    text = "text"                # a chunk of assistant text
    tool_use = "tool_use"        # the model called a tool (for the activity log)
    usage = "usage"              # usage-limit info from the provider
    result = "result"            # final answer; last event of a successful run
    error = "error"              # run failed; last event of a failed run


@dataclass
class BrainEvent:
    kind: EventKind
    text: str = ""
    session_id: str | None = None
    status: BrainStatus | None = None
    data: dict[str, Any] = field(default_factory=dict)


class Brain(Protocol):
    name: str

    def run(
        self,
        prompt: str,
        *,
        session_id: str | None,
        system: str,
        tools: list[str],
    ) -> Iterator[BrainEvent]: ...

    def health(self) -> BrainStatus: ...
