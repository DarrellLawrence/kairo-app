# Changelog

## 2026-10-13 — docs: kickoff v3 (security hardening)
- Gate fails closed: any hook error, crash, timeout or failure to start blocks (catch-all exit 2 + `onFailure: block`). Broken-hook tests.
- Model has no Google write tools: read + internal tools + `mcp_kairo.propose_action` only. Backend owns write credentials; `executor.py` runs tier 2 automatically and tier 3-4 after approval. Google MCP runs on a read-only token. Hook stays as second layer.
- `calendar.create_hold` denies attendees / anything that notifies others (field allow-list). Injection tests cover invites.
- Researched: CLI headless 24/7 on subscription. Allowed today, medium risk (section 18b).
- CLAUDE.md updated from section 14.

## 2026-10-13 — docs: kickoff v2
- Added docs/KICKOFF.md v2 (source of truth). v1 lives in OneDrive `Kairo/archive/KICKOFF_v1.md`.
- CLAUDE.md written from KICKOFF v2 section 14.
- Name is Kairo everywhere; host `kairo-home`; data `/srv/kairo`; code in WSL `~/kairo`, never OneDrive.
- New in plan: brain adapters (7b), client-ready rule (2c), relationship memory (17).
