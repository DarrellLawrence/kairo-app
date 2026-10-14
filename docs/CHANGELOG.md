# Changelog

## 2026-10-14 — fix from real use: open loops only for real people
- A mail becomes a loop only when a real person waits on your own answer. Codes, security alerts, receipts, orders, newsletters, promos, no-reply and test mails never do (rules first, then one Haiku call for the rest; unsure = no loop, one line in the brief).
- Kairo learns from your taps: Done = a person, "Not a real reply" = never from that sender again, dropped twice = muted.
- Existing junk loops are checked on the next run and closed.

## 2026-10-14 — build: PA behaviour rules (KICKOFF 13d)
- Debrief by time of day (learns when you usually ask), topic only when asked, known routine left out.
- Pushback when something important is pushed away; critical things can't be snoozed. Reply mails ping at once with a draft; promises come back next morning.
- PA job every 30 min: birthdays (gift ideas 5 days before, eve reminder, message on the day; partner and Christmas ~2 months ahead), people nudges by closeness with a reason and draft, unusual spending once with the why and again when it continues, monthly money review, study plan for tests (blocks, summary, flashcards, skipped blocks moved), patterns said once and used in the brief and reviews.
- Late-night nudge once; lighter plan after short sleep. Leaving home: only delays or weather that matters (needs `commute` in schedule.yaml).

## 2026-10-14 — build: open loops (owner's request, KICKOFF 13c)
- Kairo tracks what's unfinished (waiting mails, promises in sent mail, deadlines, tasks, school dates) and raises the most urgent one with a ready answer in your voice. Send is one tap and still an approval.
- Later brings it back by urgency, more direct each time; it closes itself when you already answered in Gmail.
- Today: "3 open" stack; brief: top 2. Max 6 proactive pings a day. Voice: "not now", "tomorrow", "in an hour", "drop it".

## 2026-10-14 — design: ember glass redesign (owner's brief)
- Warm charcoal, frosted glass, ember glow, serif greetings and numbers; all colours, glows, blur, radii, fonts and springs in the design tokens.
- Fixed: black band under the tab bar (the app now covers the whole screen; measured height only while typing).
- Floating glass tab bar (Today, Chat, Approvals, More) with a raised orb that starts a call. Board moved into More.
- Today: greeting and day summary, check-in streak, snapshot, top 3 with ticks, next up, Talk to Kairo, chips from the real day. System status moved to More → System; Today only shows a warning chip.
- Chat: greeting and chips when empty, floating input with + and mic. Call: audio-reactive orb, interrupt by talking, tap to pause, captions, mute.
- Approvals: who, what, full text, big buttons, swipe. New icon and splash.

## 2026-10-14 — build: chief of staff (v7 items 3, 4, 6)
- Kairo acts alone on what only touches the owner: trash (30-day undo), mail to his own address (locked to profile.gmail, no recipient field), moving FLEXIBLE events without guests. Sending anything to other people stays an approval card.
- Calendar types FIXED / SOCIAL / FLEXIBLE (untagged = FIXED); set by voice or chat ("gym is flexible").
- Personality: chief of staff, statements not questions, asks by importance.

## 2026-10-14 — build: voice first (v7 item 7)
- **Voice note** Shortcut: record, send, done. The PC writes it down, Kairo does it in today's chat and answers there; push only if urgent. Own-only things just happen; anything for other people waits in the app (voice notes can't make approval cards).
- **Call Kairo** Shortcut opens a live call with the mic on. Hanging up mid-sentence or while Kairo thinks no longer loses anything.
- Steps for the lock screen (iOS 18) and Back Tap in More → Capture.

## 2026-10-14 — build: model per task + usage
- Haiku for triage, sorting, summaries, reminders and calls; Sonnet for chat, writing, drafts and checks; Opus only for big planning and long reviews. Every run is counted per model; More → Usage shows it.

## 2026-10-14 — docs: kickoff v7 (chief of staff)
- Owner's 15 points added with build order: auto-merge on green CI, model per task, voice first, act alone unless it reaches another person or money, calendar types, proactive engine, personality, own voice, files to own Gmail, document gauntlet, "Done for you", privacy, read-only browsing. Tier table updated (section 4).

## 2026-10-14 — build: Milestone C, location, Milestone D
- **Jobs.** One at a time, quiet window, kill switch, daily model budget, pause on usage limit, run now from More → Jobs.
- **Email triage** (hourly 07–21). Claude sorts, the backend labels through the gate; suspicious mail is flagged and nothing else. Real-model test on the owner's three mails passed.
- **Morning brief** (06:20 weekdays, 08:20 weekends), with a plain fallback when Claude is not available.
- **Capture** by Shortcut (token) or in the app; filed every 15 minutes; Kairo asks when unclear.
- **Location** (privacy first), see KICKOFF 13b.
- **Board** tab (Kanban, timeline, goals), **School** (dates, grades, flashcards, study guides), **Lerndokumentation** (Friday push, German draft, approve).
- **Backups** every night, restore test every Sunday, restore command.
- **Reviews**: a simulated year confirms all six kinds fire on the right days.

## 2026-10-14 — docs: kickoff v6 (owner's queue, location)
- Order after C set by the owner; phase 3 moved forward. Location added (13b). Five tabs. Owner to-dos in More.

## 2026-10-14 — build: Milestone M (memory first)
- **Vault.** Fixed folders. Profile and Goals go into every chat as notes, never as instructions. The backend writes only Kairo-owned sections between markers, so the owner's text stays.
- **Onboarding.** A 25-question interview in the app, saved to the database and rendered into the vault. It can be edited or redone anytime.
- **Reviews.**
  - Schedule: daily at 21:00, plus the biggest due review on Sundays at 18:00 (weekly, monthly on the last Sunday, quarterly, half-year in June and December, yearly in December).
  - Each review is pre-filled from data. Answers go to the database, a vault note, a check-in row and tasks.
  - The push opens the form. Reviews missed while the PC was off are caught up.
- **Sync.**
  - Syncthing runs as kairo-svc and listens only on the Tailscale address. No discovery, relays or NAT.
  - The firewall allows only 100.64.0.0/10. Old file versions are kept for 30 days.
  - Kairo Logins pairs the laptop and the iPhone (Möbius Sync).
- **Email tests.** The owner's three test mails were run with the real CLI. Test 3 (injection) was flagged, nothing was forwarded and nobody was invited.

## 2026-10-14 — docs: kickoff v5 (memory first)
- New Milestone M before C: vault and sync, onboarding interview, all reviews.
- iPhone sync uses Möbius Sync (Syncthing) instead of Remotely Save/WebDAV, chosen by the owner.
- Review schedule set from daily to yearly. Voice stays in phase 3.

## 2026-10-14 — build: iPhone app design pass (before Milestone C)
- **Design system.**
  - Tokens in `web/src/design/tokens.css`: dark first plus light, one violet accent, glass, spacing 4/8/12/16/24/32, radii, shadows, SF Pro type.
  - Springs in `motion.js`: snappy, smooth, bouncy.
- **Component library** in `web/src/ui`: Button, Card, ListRow (swipe actions), Sheet (drag to dismiss), TabBar, Segmented, Badge, Toast (with Undo), EmptyState, Skeleton, ApprovalCard, ChatBubble, InputBar, SuccessCheck, Page, Stack (push/pop, edge swipe back) and Expandable (shared element). Screens use only these.
- **Motion.**
  - New dependency: the `motion` library, 13.4.6, approved by the owner.
  - Rubber-band lists and pull to refresh. Tab crossfade and slide. Bubbles spring in, streamed text fades in, typing dots.
  - Only transform and opacity are animated. Reduce Motion is respected.
- **Approvals.**
  - Swipe right to approve, left to reject, with resistance.
  - Approve and reject wait 4 s with Undo before anything is sent: a swipe can be a mistake.
  - Tier 4 always asks a second time in a sheet.
- **Standalone feel.**
  - Safe areas, no page bounce, no zoom on inputs, and the input bar sits on the keyboard (visualViewport).
  - Placeholder icon in all iOS sizes, plus splash screens for ten iPhone sizes.
- **Tabs.** The tab bar shows only what exists: Today, Chat, Approvals, More. Board and Reviews come with Milestone D.
- **Developer page.** Hidden at `#/dev/components` (More → Developer → Components).

## 2026-10-14 — build: auto-update, Start menu, Google reminders (owner's requests after the first real install)
- **Auto-update.**
  - The task `\Kairo\Kairo Auto-Update` runs as SYSTEM every minute and checks GitHub's latest release (ETag, so an unchanged answer is free).
  - It installs only after `POST /api/update/ready` says yes: no chat or job running, and not in the quiet hours unless the release notes say `[urgent]` (put `[urgent]` in the PR title).
  - Rollback stays automatic. A failed version is never retried automatically.
  - `data/updates/last-update.json` leads to one push on the iPhone (held until morning in quiet hours).
  - If Kairo itself has not answered for 30 minutes, it installs anyway.
- **Update token.** Moved to `C:\Kairo\updater` (SYSTEM + admins only), encrypted with machine DPAPI so the SYSTEM task can read it. The old per-user file is migrated on the next interactive run.
- **Token prompt.** Ctrl+V, Shift+Insert and right-click paste work. Shows how many characters arrived.
- **Kairo Logins.**
  - The Claude check is shown line by line (login file, test message as Kairo's account) with a plain reason when it fails.
  - The Google steps now include the Branding page and explain that "Publish app" needs no privacy policy or domain (only verification does). Published = no 7-day logout.
- **Google "Testing" mode.**
  - Push a reminder the day before the 7 days end, and once if Google has signed Kairo out.
  - Optional "Web application" client lets the iPhone sign in again through Kairo's tailnet address (More → Google). It can only be finished on the device where it started; the account must still match profile.yaml.
- **Start menu → Kairo:** Kairo Logins, Kairo Check, Update Kairo, Rollback, Uninstall. The broken copies in `installer\bat` are removed. A .bat no longer prints "Access is denied" when it is not admin yet.
- **BitLocker:** never an error. Windows Home is detected and gets a note about Device encryption.

## 2026-10-13 — fix: the GitHub "Source code" zip
- Running `Install Kairo.bat` from GitHub's automatic "Source code" zip (where it sits one folder too deep) now says to download `Kairo-Setup-….zip` instead. This was the likely cause of the original "window flashes and closes": PowerShell's negative exit code for a missing file skipped the old .bat's `if errorlevel 1` pause.
- Release notes say which file to download. CI checks the message.

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
