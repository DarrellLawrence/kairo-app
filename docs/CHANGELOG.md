# Changelog

## 2026-10-15 — build: open loops done right (owner's request)
- Kairo knows who you are: every name and address of yours, from the PC's profile and from Google (the Gmail account and every "send as" address). No loop, draft or card is ever addressed to you. Mail you sent yourself (Kairo's recap, files) is never read for promises, and mail from your own address never becomes a loop.
- A promise is always to the other people on the mail. Kairo keeps that mail and the people on it, so Send knows who they are.
- The right action per loop:
  - a promise to be somewhere at a time goes in your calendar with a reminder, and you get one line;
  - a promise to send or do something gets a ready message;
  - a mail gets a draft only when a reply is expected;
  - everything else is a reminder.
- Calendar truth: Kairo reads 7 days of every calendar you show, not just today and tomorrow on the main one. A read that fails says so, and nothing then says you're free or busy (the morning brief too). Once a day Kairo checks that school is in your calendar as weekly fixed blocks, and puts back any that went missing.
- Send with several people on the mail: you pick one or more (name and full address), then the normal approval. No more "send it yourself".
- Urgent only when something bad happens if you don't act today: due today, overdue, or due tomorrow before 10:00. Waiting long makes Kairo more direct, not urgent.
- A sanity check before anything is shown: Sonnet checks each loop for the right person, the right action, true facts, not a duplicate and worth your attention, then fixes it or drops it.
  - What was dropped, and why, is listed in Run diagnosis (G.2a).
  - On the first run it checks all your open loops and tells you how many were fixed and dropped.
  - Loops you add yourself need no check.
- Acceptance tests 36 (promise to attend → calendar event, no message), 37 (never addressed to you) and 38 (recipient picker).
- Removed: Kairo's "send it yourself and tap Done" answer, the 2-day primary-calendar read in drafts and the brief, the "waiting 3 days = urgent" rule, the timetable's own calendar-hold code (one shared helper now), and the broken lookup that took "p" as a message id.

## 2026-10-15 — build: fast voice (owner's priority #1)
- One Claude for the whole conversation: started when the PC starts, the app opens or a call starts, and kept until 2 hours after the last word (12 hours at most). Chat, calls and spoken messages are turns on it. No start-up per message any more.
- Models: chat, calls and voice notes run on Haiku. Drafting in your voice, judgment calls and planning go to Sonnet (by the words you use), and Haiku hands a turn to Sonnet itself when it needs to (on a call you hear "One moment."). Money stays on Opus. More → System → Voice speed shows the model of every turn.
- Slim prompt: personality, a short profile card and the conversation memory stay the same all conversation long, so the CLI can cache them. The time, what your notes say about this message and recent files go with each message. Kairo looks up the rest with its memory and vault tools.
- Calls and the chat mic stream your voice to the PC while you talk. Kairo notices you stopped after about half a second of quiet, and starts transcribing at a shorter pause already. Whisper runs on the GPU in its fast mode and finds English or German by itself.
- The answer streams back as audio, sentence by sentence: the first sentence plays while the next is made.
- Talk over Kairo on a call and it stops at once and listens. What it said so far is kept. In chat, tap the mic to stop it.
- Jobs never make you wait. A job running when you talk is stopped and tries again later; jobs wait while a call is on and for a minute after you talk.
- Hanging up keeps the conversation's Claude. If you hang up mid-sentence, what you said is answered in chat.
- Removed: the Claude start per message; the call's own warm process (closed at hang-up); for calls and the chat mic, the recording upload, the speech fetch per sentence, the sentence splitter on the phone and the talk-over level monitor; the 1.3 s silence wait.

## 2026-10-15 — plan + build: fixed destinations for the backend's own calls (owner's decision)
- Web Push, the health watch, the heartbeat and the offsite backup stay outside the gate (nothing the model chose), but only to destinations fixed in `server/app/outbound.py`. KICKOFF §14 and CLAUDE.md say so.
- Push subscriptions are accepted and sent to only on the browsers' push services (Apple, Google, Mozilla, Microsoft); the health watch calls only Kairo's own `*.ts.net` address. A test fails when any other module starts using the network.

## 2026-10-15 — plan + build: no personal data in the repo (owner's decision)
- `config/profile.yaml` holds placeholders only; KICKOFF §1, the §10 profile block and the §15 check say so. The owner's real values live only on the PC (`data\config\profile.yaml`, the vault).
- On the first start of this release Kairo copies the profile it was running with into `data\config\profile.yaml` (only what differs from the placeholders; backup in `data\backups\config-before-profile-pin-*`), so nothing changes on the PC.
- profile.yaml is no longer fingerprinted in `config/defaults-history.json`, and a migration never drops a profile value as "a default".
- Tests and examples use made-up names, places and class codes.

## 2026-10-14 — plan: KICKOFF v8 (real-use batch after P0; owner's request)
- Done now means proven on kairo-home with the real Claude and the real iPhone: `docs/ACCEPTANCE.md` (20 tests), run on the PC after every release, results in More → System → Acceptance.
- Every PR also removes what it replaces and says what it removed.
- Order: voice reply sound → voice-first capture → P1/P2 proven → capture from anywhere → judgment → semantic memory → coaching → profile import → P3–P5 → Obsidian graph → no SYSTEM writes into data (then OLAT on).
- Owner only, app-wide; no visual brain; no WhatsApp bots.
## 2026-10-14 — build: document gauntlet (v7 item 11)
- Ask in chat for a document (A4) or slides: Sonnet writes the content into Kairo's own template (serif headings, one accent colour, no gradients, icons or emojis), Kairo's Chromium renders every page/slide to an image (offline, overflow measured), Sonnet critiques the images (layout, readability, consistency, "looks AI-made?") and fixes. Max 3 rounds; "good" doesn't count while text is cut off.
- The PDF goes to vault `Kairo/Documents/` and to your own Gmail (label Kairo/School or Kairo/Work), with a push when it's ready.

## 2026-10-14 — build: mail to yourself (v7 item 10)
- Evening recap at 20:30 to your own Gmail, label Kairo/Recap: done today, still open, tomorrow, new on OLAT. Built by code, no model run. No notes about other people (item 14). Change the time or switch it off in More -> Settings.
- Files go to your own Gmail: gmail.send_self takes up to 5 vault files (read only from inside the vault, 25 MB max), labels Kairo/School and Kairo/Work. Still no recipient field: nobody else can be reached.

## 2026-10-14 — build: Kairo calls to get to know you (owner's request)
- Kairo keeps a list of what it doesn't know: questions the model queues (knowledge_ask_later) plus rules (people without a relation, close people without a birthday, no family known). Never asks for passwords, codes, PINs or bank details.
- At most one call a day, only at a good moment: after work (weekends from 10:00), before 21:00, calendar free, not at school, work or training. Declined or missed: another day. Needs 3+ questions (or one waiting 3 days).
- On the call (Sonnet): 4 topics max, smart follow-ups, double-checks contradictions, accepts corrections, stops when you say so and hangs up itself. People go to the People table and vault `60 People/<name>.md` (with birthday), facts about you to `10 Profile/Learned by Kairo.md`. After the call one line: "Saved from our call: ...". Unanswered topics come back; asked 3 times = dropped.

## 2026-10-14 — build: school portal (OLAT), read-only (owner's request)
- More -> Settings -> Logins: OLAT username and password, typed in the app only, encrypted on the PC (Windows DPAPI, bound to kairo-svc). Never in the repo, chat, logs, database or an API answer. The model has no tool for it.
- Daily at 06:05 Kairo's own browser (Playwright + Chromium, installed into tools\browser) logs in and reads: course folders, pages, announcements, calendar. New files go to vault `40 School/OLAT/<course>/`, new material and dates into the brief.
- Hard rule, enforced three ways and tested (also in a real browser in CI): never opens tests, quizzes, assignments, surveys, forums or enrollment; never clicks or fills anything but the login; after login the browser aborts every request that isn't a page load, any other site and any test/quiz/assignment address.
- Timetable: found on OLAT, or from a photo/PDF you send once (Kairo asks if it can't find it). Becomes one weekly FIXED "School" block per school day until the semester ends. Calendar holds may now repeat weekly, only with an end within a year (gate-tested).

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
