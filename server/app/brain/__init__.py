"""Model adapters. Pick one with KAIRO_BRAIN (KICKOFF 7b)."""

from __future__ import annotations

from ..settings import Settings
from .base import Brain, BrainEvent, BrainStatus, EventKind


def make_brain(settings: Settings, *, mcp_config=None, hook_env: dict[str, str] | None = None) -> Brain:
    if settings.brain == "claude_cli":
        from .claude_cli import ClaudeCLIBrain

        return ClaudeCLIBrain(
            claude_bin=settings.claude_bin,
            workspace_dir=settings.workspace_dir,
            mcp_config=mcp_config,
            timeout_s=settings.brain_timeout_s,
            extra_env=hook_env,
        )
    raise ValueError(f"Unknown KAIRO_BRAIN={settings.brain!r}. Built so far: claude_cli")


__all__ = ["Brain", "BrainEvent", "BrainStatus", "EventKind", "make_brain"]
