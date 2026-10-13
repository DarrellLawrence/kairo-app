# Changelog

## 2026-10-13 — fix: "Install Kairo.bat" closed instantly on a real PC
- All .bat files now ask for admin rights themselves (no more elevation from inside PowerShell, whose window closed without a message on an early error), find their files from their own folder, use the full path to Windows PowerShell, and unblock files Windows marked as downloaded.
- They never close on an error: plain message, error code, log path, wait for a key. The scripts catch every error (also module-load errors) and log to `C:\Kairo\install.log` (owner + admins only).
- `Assert-Admin`: elevated exit code no longer comes back empty (= success).
- CI installs via the .bat from `Downloads\Kairo Setup (1)\...` with every file blocked and execution policy AllSigned; also checks a broken setup and a lone .bat keep the window open with a message. Static test: .bat/.ps1 files are ASCII, CRLF, no BOM.

## 2026-10-13 — build: Milestone B (the gate)
- `gate.py` + `actions.py`: default deny, per-action schemas (unknown fields refused), tiers can only be made stricter by config, holds allow-list, Kairo/ labels only, jobs can't make cards, limits.
- PreToolUse hook fails closed (every error = exit 2; `onFailure: block`); verified with the real CLI, including the control that a missing hook WITHOUT onFailure lets a tool run.
- `mcp_kairo` (only MCP server) -> `/internal/tools/call` (per-start token, loopback) -> gate -> tools / `executor.py` / approval card. Google read and write tokens; only `executor.py` creates a write client (test-enforced).
- Approval cards: exact resolved args, backend-built summaries, edit re-checked by the gate, second confirm for tier 4, 48 h expiry, limits re-checked; Approvals screen; Web Push (quiet hours respected).
- Append-only audit log (DB triggers) + Activity screen; kill switch stops a running call.
- Google sign-in (PKCE, account must match profile.yaml) + installer walkthrough.
- Real-model acceptance tests: injection (forward, invite), money, approved = exact.

## 2026-10-13 — build: Windows hardening (before Milestone B)
- Kairo runs as its own limited Windows account `kairo-svc` (no admin, hidden, sees only C:\Kairo).
- Claude Code: pinned official Windows binary from npm, SHA-512 checked, in C:\Kairo\tools.
- Boot check after every reboot (Claude login, Tailscale, HTTPS) shown on the More screen; `Kairo Check.bat`.
- `/api/selftest` (only from the PC itself). CI tests the account, the probe and the boot check on Windows.

## 2026-10-13 — docs: kickoff v4 (Windows host, one-click install)
- kairo-home stays on Windows; Kairo runs natively (no Docker/WSL there).
- Deploy = release zip + `Install-Kairo.bat` (install, update, auto-rollback, guided logins); boot task without stored password; no sleep.
- Google tools live in the backend; `mcp_kairo` is the only MCP server and forwards to the backend. No third-party Google MCP server.
- Owner config + secrets live in the data folder and survive updates.
- Supersedes the Milestone A decision "CLI inside the container".
## 2026-10-13 — build: Milestone A (skeleton)
- Backend: `/health`, SQLModel models for section 9, Alembic initial migration, chat threads + websocket, one brain call at a time.
- Brain: `brain/base.py` + `brain/claude_cli.py`. Flags verified on Claude Code 2.1.296 with real runs. Prompt via stdin (never argv). The CLI process gets an allow-listed environment, so backend secrets can't reach it.
- Web: Vite 8 + React 19 + vite-plugin-pwa, six-tab bar, Chat over websocket.
- Ops: Dockerfile + compose (port bound to 127.0.0.1 only), `scripts/deploy.sh` (+ `--rollback`, auto-rollback on failed health check, DB backup per deploy), `scripts/setup-kairo-home.sh`, CI.
- Build decisions (not in KICKOFF, proposed for v4):
  - **Claude Code CLI runs inside the API container** (pinned npm version), not on the host. The backend has to spawn it, and a container can't run a host binary. The login lives in `/srv/kairo/claude-config` and is done once with `docker compose run --rm -it api claude`.
  - Extra table `chat_messages` (the Chat screen needs history to show).
  - Extra brain status `unavailable` (binary missing or crashed).
  - No `@vitejs/plugin-react`: Vite 8 handles JSX itself.

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
