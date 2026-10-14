# Kairo diagnosis (2026-10-14)

Read-only audit. No code changed. Line numbers refer to branch `claude/vigilant-keller-wojdpl` at `d5ec42f`.

## 0. What could and couldn't be checked

- **kairo-home was not reachable from this audit.** It ran in a cloud container with no Tailscale and no SSH; outbound requests got proxy 403. That matches the design: no public ports, served only through `tailscale serve` (CLAUDE.md).
- So **every number from the live DB, logs, config or updater is UNVERIFIED.** The appendix (section G) has the exact commands to get each one on kairo-home.
- **Verified here:** source code, the local test suite (571 passed, 21 skipped), GitHub CI runs (64 since 2026-10-13 12:35Z) and GitHub releases (13).
- **What's deployed, per GitHub:**
  - Latest release: `v2026.10.14.0942-3c8d6aa` (PR #16).
  - PR #17 was merged at 09:45Z; its release was still pending while this was written.
  - PR #18 (knowledge calls, recap, gauntlet) is open and not merged.
  - Whether kairo-home actually installed any of these is UNVERIFIED (see G.6).
- The tables added in #17/#18 (`portal_items`, `knowledge_items`, `documents`, and `email_triage.personal_reply`) **don't exist on kairo-home yet** unless #17 was installed.

---

## A. Approvals empty, open loops full

### A1. Every code path that creates an approval card

There is only one constructor: `approvals.create_card` (`server/app/approvals.py:117-129`). It refuses anything that isn't an `approve` verdict and stores the card with `status="open"`. It has two callers:

| # | Caller | Trigger | Origin | Card ends up | Wired? |
|---|---|---|---|---|---|
| 1 | `main.py:internal_tool_call` (390-432, create at 423) | Model calls `mcp__kairo__propose_action` with a tier-3/4 action | `runner.current_origin` | **Open** in Approvals, plus a push (`on_new_card`, main.py:164-165) | Yes, but only from **app chat** (`owner_chat`) |
| 2 | `main.py:loop_send` (1041-1075, create at 1066) | Owner taps **Send** on an open loop | hardcoded `owner_chat` (1061-1062) | **Approved and executed in the same request** (`approvals.approve` at 1069); never shown in Approvals | Yes |

How cards are decided:
- `approvals.approve` (164-212) and `approvals.reject` (215-226), via `POST /api/approvals/{id}/approve|reject` (main.py:535-552).
- `expire_old` (134-145) expires open cards after 48 h (`EXPIRY`, approvals.py:28).
- The model has no approve or reject tool (`actions.py` DIRECT_TOOLS, `mcp_tools.py:tool_list`).

Who can't create cards:
- `gate.py:_decide_proposal` (122-123) denies tier 3/4 for any origin other than `owner_chat`.
- So jobs (triage, PA, promises, brief) and voice notes **can never create cards**.
- Job model calls get no tools at all (`chat.py:ask_sync`, `tools=[]`, 90-102).

### A2. Every code path that creates an open loop

There are two constructors: `loops.upsert` (loops.py:258-277; never reopens a closed loop) and `loops.add` (280-289).

| Creator | kind / source | Trigger | `who_email` (Send target) |
|---|---|---|---|
| `tools.py:_loops_add` → `loops.add` | promise/waiting/task/deadline / chat | Model tool `loops_add` (chat, voice note) | none (Send → 400) |
| `main.py:loop_add` (980-989) | same / chat | `POST /api/loops` | none |
| `loops.sync` (487-489) | reply / email | `open_loops` job every 15 min, and after `email_triage` (main.py:233-240) | From header |
| `loops.sync` (492-498) | deadline or task / task | Every Task due ≤7 days, or priority ≥2 | none |
| `loops.sync` (500-505) | deadline / school | SchoolEvent test/deadline ≤7 days | none |
| `loops.PromiseFinder.run` (837-840) | promise / promise | `promises` job, hourly 07-21 | `to_email` **from model output** |
| `pa.py:birthdays` (144-157), `_christmas` (177-179) | birthday | `pa` job every 30 min | `Person.email` |
| `pa.py:people` (242-243) | people | `pa` | `Person.email` |
| `pa.py:money` (304-305), `monthly_review` (359-360) | money | `pa` | none |
| `pa.py:tests` (455), `_skipped` (493-494) | study | `pa` | none |
| `pa.py:patterns` (527) | pattern | `pa` | none |

That's 6 automatic producers (sync ×3, promises, PA ×5 kinds) against 2 manual ones. The noise filter from PR #17 (`check_replies`, `personal_reply`) only covers the **email reply** branch (475-476). The task, school, promise and PA branches have no relevance filter.

### A3. Every way something can reach another person

`Executor.run` (executor.py:36-44) does **no** tier, approval or pause check of its own. It picks the handler by action name.

| Action | Reaches others? | Tier | Path | Approval record? | permissions.yaml? | PreToolUse hook? | propose_action? |
|---|---|---|---|---|---|---|---|
| `gmail.send` | **yes** | 3 | chat → card; loop Send → card+approve | yes (card) | yes | **chat path only** (CLI tool call); loop Send is backend-only | chat: yes; loop: no (backend builds the proposal itself, main.py:1054-1058) |
| `gmail.reply` | **yes** | 3 | same | yes | yes | chat only | same |
| `calendar.respond` | **yes** (`sendUpdates=all`, executor.py:138-148) | 3 | chat → card only | yes | yes | yes | yes |
| `calendar.delete` | **yes** if the event has guests (`send_updates` set, approvals.py:92-94) | 4 + confirm | chat → card only | yes | yes | yes | yes |
| `gmail.create_draft` | no (a draft to anyone, never sent) | 2 | runs at once under **any origin** (gate.py:120-121 comes before the origin check at 122) | no | yes | chat only | yes |
| `calendar.create_hold` / `move` | no (no attendee field, `sendUpdates=none`, FLEXIBLE-only move refuses events with guests, executor.py:113-122) | 2 | any origin | no | yes | chat only | yes |
| `gmail.send_self` | no (recipient = `profile.gmail`, no field) | 2 | recap, gauntlet, chat | no | yes | chat only | yes |

**On kairo-home the hook and `mcp_kairo` are missing (A5.1)**, so the whole "chat" column is not reachable in production today.

The backend paths call `gate.decide` themselves and **never go through the hook**: triage labels, PA study holds, timetable holds, recap, gauntlet mail and `loop_send`. The hook only runs inside the Claude CLI process (`workspace.py:27-34`, `hooks/pre_tool_use.py`). Lerndokumentation's "approve" sends nothing (no executor or propose in `lerndoku.py`).

### A4. DB counts, last 14 days: UNVERIFIED

These must be run on kairo-home: queries **G.1** (approvals by status) and **G.2** (open loops by kind × source × status). The design makes one prediction you can check: approvals with `reason LIKE 'Open loop%'` are all `done`/`failed` and none stay `open`, while loops come from 6 automatic producers.

### A5. Why Approvals is empty, in 3 lines

1. **No release has ever shipped Kairo's tool bridge.** `scripts/build-release.sh:39` copies `server/app`, `migrations` and `alembic.ini` but not `server/mcp_kairo/` or `server/hooks/`, and that line has been the same since the script was created (`ea9de54`). Verified by building the zip: 133 entries, no `mcp_kairo`, no `pre_tool_use.py`. On kairo-home the chat model has no `propose_action` (or any Kairo tool), so path #1 can't create a card.
2. The only other path, loop **Send**, creates *and* approves the card in the same request (`main.py:1066-1069`), so those cards never sit in Approvals.
3. By design, jobs and voice notes can't create cards (`gate.py:122-123`), and everything that only touches you is tier 2. On top of that, `data\config\permissions.yaml` on kairo-home is the first-install copy that updates never refresh (`installer/Kairo.psm1:235-241`). A first-release install lacks 23 of today's 38 actions.

---

## B. Architecture map

### B1. Components and processes on kairo-home (from the installer code; live state UNVERIFIED, see G.6)

**Scheduled tasks** (Task Scheduler folder `\Kairo\`):

| Task | Runs as | Trigger | Runs | Source |
|---|---|---|---|---|
| `Kairo` | kairo-svc, limited | at boot, plus a 5-min watchdog; restarts up to 999× | `app\current\venv\Scripts\python.exe -m app.serve --env-file data\kairo.env` | `Kairo.psm1:Register-KairoTask` 484-498 |
| `Kairo Boot Check` | SYSTEM | at boot + 3 min | `Kairo-BootCheck.ps1` (health, account, real Claude selftest, Tailscale, serve mapping, HTTPS) → `data\logs\boot-check.json` | `Kairo.psm1:503-515`, `Kairo-BootCheck.ps1` |
| `Kairo Auto-Update` | SYSTEM | **every 1 min** | `Kairo-AutoUpdate.ps1`: GitHub releases/latest (ETag) → ask `/api/update/ready` → `Update-Kairo.ps1 -Auto` | `Kairo.psm1:520-531` |
| `Kairo Sync` | kairo-svc | at boot, plus a 5-min watchdog | `tools\syncthing\syncthing.exe` (one folder `data\vault`; GUI on 127.0.0.1:8384; sync on the Tailscale IP:22000; firewall allows only 100.64.0.0/10) | `KairoSync.psm1` |

**Inside the app:**
- uvicorn listens on **127.0.0.1:8000** only (`serve.py:17, 68-76`). Tailscale `serve --bg 8000` publishes it as `https://<node>.ts.net` (`KairoSetup.psm1:223-228`).
- In-process: the APScheduler scheduler, housekeeping every 60 s, and the voice models.

**Child processes:**
- One `claude.exe` (pinned **2.1.296**, `installer/versions.json`; npm tarball with a SHA-512 check, `KairoSetup.psm1:46-72`) per brain call. It in turn tries to start `python -m mcp_kairo` and the hook, which aren't in the release (A5.1).
- Chromium through Playwright, for OLAT and the gauntlet (#17/#18).

**Accounts:**
- `kairo-svc`: standard user; batch logon only; denied local and RDP logon. **Its password is reset on every install and update** (`Kairo.psm1:Initialize-ServiceAccount` 153-157, called from `Install-Kairo.ps1:67`).
- Updater and boot check run as SYSTEM.

**Where the updater and installer log:**
- `C:\Kairo\updater\auto-update.log` (SYSTEM/admin only, rotated at 1 MB)
- `C:\Kairo\updater\state.json` (etag, failed list)
- `C:\Kairo\install.log` (transcript, appended forever)
- `C:\Kairo\data\updates\last-update.json`

**How often the updater ran, installed, rolled back or failed: UNVERIFIED** (G.6 and those logs). GitHub side: **13 releases** published between 2026-10-13 13:32Z and 2026-10-14 09:42Z.

### B2. Data stores

| Store | What | Writes | Reads |
|---|---|---|---|
| `data\db\kairo.sqlite` | Everything (tables below) | app (kairo-svc); alembic as admin/SYSTEM | app; hook (kill switch, open cards, outbound count, `gate_context.py:19-31`) |
| `data\vault\` | Obsidian vault; Profile and Goals go into **every** system prompt (`vault.py:context` 39-67) | app (`vault.write_section`, `notes_write`, OLAT files, gauntlet PDFs), Syncthing | app, model (`vault.read`), Syncthing |
| `data\config\` | profile, permissions, schedule, personality, voice.md | **Seeded once, never updated** (`Kairo.psm1:Initialize-Config` 235-241) | app, gate, hook |
| `data\secrets\` | google-read.json / google-write.json (**plaintext** tokens), google clients, vapid-private.pem, capture/location tokens, syncthing-gui.txt (plaintext), logins\olat.bin (per-user DPAPI) | app, installer | app (write token only in `executor.py`); **owner and kairo-svc can read all of it** (Modify ACL on `data`, `Kairo.psm1:131`) |
| `data\claude-config\` | Claude login and CLI transcripts | claude.exe | claude.exe; **not backed up** |
| `data\workspace\` | `.claude\settings.json` (hook), `.mcp.json` (per-start token), system prompt | app at every start and call | claude.exe |
| `data\inbox\` | Raw captures and voice-note audio | capture | capture job, backup; **never pruned** |
| `data\logs\` | `kairo.log` (5×5 MB; uvicorn access lines include query strings, not bodies), `boot-check.json` | app, boot check | owner |
| `data\backups\` | Pre-update DB copies (20 kept); `nightly\*.zip` (DB + vault + config + inbox, **no secrets, unencrypted, same disk**; 14 daily + 8 weekly) | installer, nightly job 03:30 | restore test, Sundays 04:00 |
| `data\models\`, `tools\voice`, `tools\browser`, `tools\claude`, `downloads\` (never cleaned) | Binaries and models | installer/updater (SYSTEM) | app |
| `updater\github-token.bin` | GitHub token (machine DPAPI) | owner during Logins | updater |

**DB tables** (`server/app/db.py`):
- goals, projects, tasks
- location_events
- captures
- email_triage
- briefs
- approvals
- audit_log (append-only by trigger)
- job_runs
- checkins, reviews
- onboarding_answers
- school_events, grades, flashcards
- lerndoku
- calls
- transactions, health_days, people, chores, shopping_items
- model_runs
- chat_threads, chat_messages
- push_subscriptions
- settings (key-value: kill switch, budget, prefs, …)
- calendar_tags
- open_loops
- From #17/#18: reply_senders, portal_items, knowledge_items, documents

### B3. Scheduled and background jobs

**How the scheduler works** (`scheduler.py:Jobs.start` 321-355):
- One APScheduler `BackgroundScheduler`, with `coalesce`, `max_instances=1` and a 6 h misfire grace.
- **Only one job runs at a time.** `run_job` takes a global lock and waits up to 30 min for it (`QUEUE_WAIT_S`, 37, 217). The time spent waiting is never recorded.
- The quiet window is 23:00-06:00 (`config/schedule.yaml:3`). Every job except backup and restore_test is skipped in it, unless the run is forced (224-225).
- Model budget: 60 model calls a day for jobs and in-app helpers (`ask_brain` 187-208). Chat, calls and voice notes are not counted.

**Runtimes are UNVERIFIED.** They come from `job_runs.finished_at - started_at`; see G.3.

| Job | What | When | Model calls (purpose → model, `models.py:DEFAULTS` 18-25) |
|---|---|---|---|
| email_triage | Sorts new inbox mail, applies labels, then runs open_loops | hourly 07-21 | triage→haiku ×1; then everything open_loops does |
| open_loops | Reply check, sync, then pushes 1 due loop | every 15 min | reply_check→haiku; draft→sonnet; up to 2× voice_check→sonnet (`writer.py` 78-106) |
| promises | Finds promises in sent mail | hourly 07-21 (:10) | promises→haiku |
| pa | Birthdays, people, money, tests, patterns | every 30 min | draft→sonnet (gift ideas, messages) + voice_check→sonnet; summary→haiku (money review, days 1-5); **study guide and cards run as draft→sonnet** (no purpose given, `pa.py:443-444`) |
| morning_brief | Brief push or call | weekdays 06:20, weekends 08:20 | brief→haiku ×1 |
| capture_process | Voice memos (local Whisper), letters, captures | every 15 min | capture→haiku per letter (with image) + 1 batch call |
| review.daily / review.big | Evening check-in, Sunday review | 21:00 / Sun 18:00, forced (runs even in the quiet window) | none |
| lerndoku_prompt | Friday reminder | Fri 17:30 | none |
| backup / restore_test | Nightly backup, weekly restore test | 03:30 / Sun 04:00 | none |
| knowledge_call (#18, not merged) | Getting-to-know-you call | every 30 min | none in the job; the call itself uses learn_call→sonnet |
| evening_recap (#18) | Recap mail | 20:30 | none |
| olat (#17) | Reads OLAT | 06:05 | document→sonnet only when a new timetable is found |
| document (#18) | Gauntlet | on demand | document→sonnet + gauntlet→sonnet, up to 7 calls |

**Not scheduled jobs, but always running:**
- Housekeeping every 60 s: update notice and Google reminder (`housekeeping.py:128-132`).
- Voice warm-up once at start (#16).

**Never-read config keys:** `jobs.evening_checkin`, `jobs.weekly_review` and `capture_process.only_if_inbox_non_empty` in `config/schedule.yaml:12-14`.

**Missed runs are lost:** jobs live in APScheduler's in-memory store, so a run missed while the PC was off never happens. Only the reviews have a catch-up (`catch_up`, 303-309). The docstring (scheduler.py:11-12) claims otherwise.

### B4. Push notifications

`Pusher.notify` (`push.py:74-81`):
- **Quiet hours drop the push. It is not queued and never sent later.**
- Pushes are **not recorded in the DB**; a successful delivery isn't logged anywhere.
- The only per-source count is `audit_log.action='proactive.ping'`, which covers open-loop pings and leaving notices.
- **So counts per source can't be rebuilt from the DB.** G.4 gives the counts that are possible plus proxies.

| Source (file:function) | Trigger | Limit / dedupe |
|---|---|---|
| `loops.py:LoopEngine.run` 655 | 1 due loop every 15 min | 6/day budget (`DEFAULT_MAX_PINGS`, loops.py:45). **New reply loops skip the budget** (630-634). Not during a calendar event unless urgent |
| `main.py:_leaving_check` ~1250-1290 | Leaving home with a delay or bad weather | Shares the budget. The audit row is written even when the push is then dropped in quiet hours |
| `main.py:location_event` → `location.arrival_reminder` | Arriving at a place with tasks | **None** (no budget, dedupe or audit) |
| `brief.py:make` 215 | Every brief run | none |
| `scheduler.py:_daily_review_body` 283 / `_big_review_body` 292 | Review created | once per period |
| `main.py:on_new_card` 164 | Model creates a card | none |
| `triage.py` 135 (`on_flag`) | Suspicious mail | once per mail |
| `capture.py` 259/361 (`on_question`) | Unclear capture or letter | per capture |
| `main.py` backup/restore failure (205, 220) | Failure | none |
| `main.py:lerndoku_prompt_job` 229 | Friday | weekly |
| `voice/calls.py:start` 28 | Brief/check-in as a call, "Call me now" (`ignore_quiet`) | none |
| `voicenote.py` 152 | Voice-note reply starting with "URGENT:" | per note |
| `housekeeping.py:_push` 47 | Update installed; Google sign-in expiring | Deferred (not dropped) in quiet hours; flags in files |
| #17/#18: OLAT timetable ask, timetable done, gauntlet done, knowledge call, call summary | each event | once ever / per item / 1 a day |

### B5. Brain calls

- **Started cold for every call.** `ClaudeCLIBrain.run` starts a new `claude -p` process each time (`brain/claude_cli.py:203-212`).
  - Flags: `build_command` 126-151.
  - Every process also starts `python -m mcp_kairo` (`--mcp-config`), even for jobs that have no tools.
  - Every tool call starts a Python hook process (`workspace.py:27-34`).
- **There is no token streaming.** `--include-partial-messages` is not passed (126-151). Text arrives as whole assistant blocks (`claude_cli.py:275-278`), so "first token" really means "first complete text block".
- **One model call at a time across the whole app.** `BrainRunner._lock` (`chat.py:25, 61, 94`) has no timeout, so chat waits behind a job's model call and jobs wait behind chat.
- **Model per job:** see B3. Chat uses sonnet, calls use haiku, voice notes use sonnet; owner settings can override.
- **Logged per call** in `model_runs` (`models.py:record` 37-42):
  - `ts` (end of the call), `purpose`, `model`, `ok`, `duration_ms` (the CLI's own figure, which excludes lock wait), tokens and `cost_usd`.
  - Adapter errors (timeout, can't start) are stored with NULL model and duration. Exceptions write **no row at all** (`chat.py:68-69`).
- **Average and p95 latency, and usage per model over 14 days: UNVERIFIED.** See G.5, which computes them from `model_runs`. The More → Usage screen shows runs, minutes, tokens and $ by model and by job (`main.py:/api/usage`, `models.py:usage` 50-68).

---

## C. Measured performance

**No measurement could be taken: kairo-home was unreachable.** This table shows what is logged at all:

| Metric | Logged? | Where / how to get it |
|---|---|---|
| Chat: message → first token | **Not measured.** No server or client timing, and no token streaming (B5) | Proxy: `chat_messages` owner `created_at` → kairo reply `created_at` (total time, not first token). G.5 |
| Call: end of speech → Kairo speaks | **Not measured** | Proxy: `model_runs` purpose `call` duration. Missing from that: STT, lock wait, TTS synthesis and WAV download. The turn is fully serial: STT on the whole recording → full reply (`Call.jsx:ask` resolves only on `done`, 119-121) → full TTS WAV (`voice.js:speak` 55-60) → playback |
| Voice note: upload → action done | Proxy only | `captures.created_at` → `captures.filed_at` (source `voice_note`/`app_voice`). G.5 |
| STT time per request | **Computed, not stored** | `Transcript.took` is returned in the `/api/voice/transcribe` JSON only (main.py ~1590) and discarded in voice notes and captures |
| TTS time per request | **Not measured** | – |
| Job runtime | Yes | `job_runs` (queue wait not included). G.3 |
| Updater: runs, installs, rollbacks, failures | See B1 and G.6 | – |

---

## D. Problems found

Format: **what**, where, evidence, impact on you (H/M/L), effort to fix (S/M/L).

### D1. Bugs

1. **Noise mails still become loops through Tasks.** When triage marks a mail "reply" with a deadline, it creates a Task (`triage.py:122-127`). `loops.sync` then turns every Task due within 7 days into a `deadline` loop (`loops.py:492-498`), with no reply check. The #17 filter only covers the email branch (475-476). Evidence: code path; unit tests only cover the email branch (`test_loops.py`). **H / S**
2. **The kill switch doesn't stop approving a card.** `approvals.approve` (`approvals.py:164-212`) and `POST /api/approvals/{id}/approve` (`main.py:535-544`) never check `killswitch.is_paused`. An edited card is re-gated with a hardcoded `paused=False` (`approvals.py:180`). There's no test for approving while paused. **M / S**
3. **One school test makes two loops.** `pa.py:tests` 455 creates a `study` loop and `loops.sync` 500-505 creates a `deadline` loop, from different sources, so dedupe never matches them. **M / S**
4. **One mail with a deadline makes two loops:** the reply loop (email) plus the deadline loop (from its Task, #1). **M / S**
5. **Promises said in chat and in mail are tracked twice.** `loops_add` uses ref `chat:<ts>`; `PromiseFinder` uses `msgid:what` (`loops.py:837`). There's no cross-dedupe. **L / S**
6. **Runs missed while the PC was off are lost** (brief, triage, PA). APScheduler uses its in-memory store. The docstring claims catch-up (`scheduler.py:11-12`), but only the reviews have it (303-309). **M / M**
7. **Pushes in quiet hours are dropped, not deferred** (`push.py:74-78`). The leaving-notice audit row is written even when the push is dropped, so it eats ping budget for nothing (`main.py:1289`). **L / S**
8. **Loop Send can strand a card or be refused.**
   - If gmail.send is made tier 4, the card is created, then `approve` fails without a confirm and the card stays open (`main.py:1066-1071`, `approvals.py:167`).
   - With 10 cards open, Send is refused (`gate.py:124`). **L / S**
9. **The audit log names the wrong actor.** `create_card` always writes actor `brain` (`approvals.py:127`), even when you tapped Send. **L / S**
10. **Config keys that do nothing.** In `config/permissions.yaml`, `rules.calendar.create_hold.calendar/title_prefix/deny_if_present` are never read; only `allow_only_fields` is (`gate.py:196-197`). In `schedule.yaml`, `jobs.evening_checkin` and `weekly_review` are never read. **L / S**
11. **PA study guides and cards run under purpose `draft`, not `study`** (`pa.py:443-444`), so the Usage page lumps them under drafts. **L / S**
12. **The Jobs screen fills with "skipped (quiet window)" rows.** Interval jobs write a JobRun row every time they're skipped in the quiet window, about 84 rows a night across 4 jobs (`scheduler.py:224-225, 250-256`). **L / S**
13. **The CI voice test is flaky.** The base Whisper model hears "Mathe" as "Matte" (`test_voice.py:167`; runs 38521092649 and 38524163762). It failed on two main pushes. **L / S**

### D2. Overlaps / duplicate features

- **Five summary surfaces:**
  - morning brief (`brief.py:make`, push 06:20)
  - debrief tool (`debrief.py:gather`, on request)
  - Today screen (`main.py:/api/today` 1077)
  - evening check-in (`scheduler.py:_daily_review_body`, 21:00)
  - evening recap mail (#18 `recap.py`, 20:30)

  The recap and the check-in are 30 minutes apart and cover the same day.
- **Brief duplicates itself.** The same waiting mail can appear twice in one brief: under "Needs a reply" (`triage.needs_reply`, brief.py:116) and under "Still open" (`open_loops`, 135).
- **Loops vs tasks vs promises vs reminders vs school.**
  - Every soon or important Task is mirrored as a loop (`loops.py:492-498`).
  - School tests get a deadline loop, a study loop and study calendar holds (`pa.py:_hold`).
  - Promises come in from two producers (#5).
  - Location reminders on tasks are a separate push path with no budget (`main.py:1246`).

  So one real-world item can appear in up to 3 places.
- **Calls:** brief-as-call, check-in-as-call, "call me now" and knowledge calls (#18) each have their own trigger (`voice/calls.py`, `knowledge.py`). There's no shared "max calls per day".
- **Two sources of quiet hours.** `profile.quiet_hours` (push) and `schedule.quiet_window` (jobs) are only unified once the owner saves a setting (`prefs.py:196-199`).

### D3. Lag (code facts; real numbers UNVERIFIED, see G.5)

- **A cold CLI start on every message** (`claude_cli.py:203`), plus a `mcp_kairo` Python process per call and a hook process per tool call. **H / L**
- **No token streaming.** `--include-partial-messages` isn't passed (`claude_cli.py:126-151`), so the reply shows up as whole blocks. Check that the flag exists in the installed CLI version first. **H / S**
- **One model call at a time, app-wide** (`chat.py:25`, no timeout). Chat waits behind job calls: up to 3 Sonnet calls per loop draft (draft + 2 voice checks, `writer.py:78-106`), PA gift ideas, and the gauntlet (up to 7). **H / M**
- **One job at a time** (`scheduler.py:217`). A long job (capture with local Whisper, OLAT, gauntlet) delays open_loops and triage by up to 30 min, after which they're skipped. **M / M**
- **Call turns are fully serial:** STT on the whole recording, then the whole reply, then the whole TTS WAV (`Call.jsx` 119-121, `voice.js:speak` 55-60). Learning calls run on Sonnet (#18). **H / M-L**


### D4. Silent failures

| Failure | Detected? | Are you told? |
|---|---|---|
| **Kairo tools missing in the release** (A5.1) | No. The CLI's `system/init` event is read only for `session_id` (`claude_cli.py:273`); MCP server status is ignored; the selftest uses `tools=[]` (`main.py:702`) | **No** |
| Google token expiry (Testing mode, 7 days) | Yes (`housekeeping.py:google_reminder` 84-113) | Push the day before (deferred in quiet hours) |
| Google token revoked / password changed | Only when a call fails. Tokens marked `no_expiry` are never re-checked (`housekeeping.py:92`). Triage turns into a silent "skipped" (`triage.py:69-87`) | **No push**; More still shows Google OK (it only checks the files exist) |
| Claude logged out | Only when a run fails (`claude_cli.py:classify_error` 88-94) | No push; chat banner, More, Today. Jobs keep spending budget. The "warn 3 days before" from KICKOFF 18a-2 doesn't exist |
| Claude usage limit | Yes; jobs pause 1 h (`scheduler.py:204-207`) | No push; More and the chat banner |
| Tailscale down / node-key expiry | Only the boot check, after a boot (`Kairo-BootCheck.ps1:45-65`) | **No.** Syncthing falls back to 127.0.0.1 if Tailscale is down during an update (`KairoSync.psm1:70-77`) |
| Syncthing conflicts | **Not detected** (nothing reads `.sync-conflict-*`). Conflict copies in Profile/Goals are `.md` files, so they go into every system prompt (`vault.py:47`) | No |
| Disk full | **No free-space check anywhere**; `/health` only runs `SELECT 1` | A "Backup failed" push is attempted, but the backup (03:30) and restore test (04:00) run in quiet hours, so the push is **dropped** (`push.py:75-77`). It shows on Today / in the brief. `.partial` files are never cleaned (`backup.py:67-92`) |
| Kairo crash loop | The task restarts it 999×. **No heartbeat** (KICKOFF:304 not built) | No |
| Update failed / rolled back | `updater\auto-update.log`; the version is added to `failed` | Push once (`housekeeping.py:update_notice` 58-67); the app shows no version history |
| OLAT login / DPAPI unreadable (#17) | Report in settings | Logins page only |

### D5. Security gaps

1. **kairo-svc can probably run code as SYSTEM.** `data\kairo.env` is Modify for kairo-svc (`Kairo.psm1:85, 131`). The SYSTEM updater loads every line into its own environment (`Use-KairoEnv` 270-273), then runs `python -m app.voice download` and `alembic` (349, 402). Unknown keys are kept (`Write-EnvFile` 244-256). A `PYTHONPATH=` line means code execution as SYSTEM at the next update. It needs a prior compromise of the app, but it breaks "kairo-svc never admin". **H if exploited / S**
2. **Your elevated Claude login runs in a folder kairo-svc writes.** `Open-ClaudeLogin` starts `claude.exe` in the installer's admin session with `WorkingDirectory=data\workspace` (`KairoSetup.psm1:245`). The app rewrites that folder's `.claude\settings.json` hooks (`workspace.py:50-56`). Same class as #1. **M / S**
3. **Loop Send can mail an address that came from untrusted content, through the model.**
   - Promise loops: `who_email` is `to_email` from model output, never checked against the sent mail's real To header (`loops.py:837-840`).
   - Birthday and people loops use `Person.email`, which the model sets with the tier-1 `people_note` (`tools.py:_people_note`, `life.py:people_note`).
   - The UI shows only a name (`Loops.jsx:38, 62`); one tap creates *and* approves the card (`main.py:1066-1069`).

   This conflicts with CLAUDE.md: "No code path may let such content create or approve an approval". **H / S-M**
4. **The kill switch isn't enforced on approve** (D1.2). **M / S**
5. **Tier 2 executes under every origin, before the origin check** (`gate.py:120-121`). In chat, a model reading an injected mail can trash mail (30-day undo), create drafts to anyone, or add holds or moves, with no card. That's the v7 design, but nothing records that it was content-driven. **L-M / M**
6. **The API trusts any device on the tailnet.** `/api/*` has no per-request auth; requests without an `Origin` header pass (`main.py:52-62`). Your tailnet ACLs are UNVERIFIED. **L / M**
7. **Secrets at rest.** Google tokens and the Syncthing GUI password are plaintext JSON/text; both the owner and kairo-svc can read them (`Kairo.psm1:131`). The DPAPI'd OLAT password is **likely unreadable after every update**: an admin password reset makes per-user DPAPI data unreadable (`secretbox.py:33` + `Initialize-ServiceAccount`; UNVERIFIED on the PC, to check: save an OLAT login, run Update, open Logins). **M / S**
8. **A BitLocker recovery key would end up in `C:\Kairo\install.log`** (`KairoSetup.psm1:175-178` printed during `Start-Transcript`, `Install-Kairo.ps1:41`). Only if the installer actually enabled BitLocker (UNVERIFIED; Home edition likely skipped it). **L / S**
9. **The real hook was never proven to fail closed with the real CLI on Windows.** `test_realcli.py` (e)/(f) are skipped in CI, and the hook file isn't even shipped (A5.1). **M / S**

### D6. Updater / release risks

1. **The release doesn't contain `server/hooks` or `server/mcp_kairo`** (A5.1). The CI install test can't notice: it uses a fake CLI and a `tools=[]` selftest (`installer/ci/Test-Install.ps1`). **H / S**
2. **Owner config is never updated** (`Initialize-Config` 235-241). New actions, PA and answer-first rules (`personality.md` +55 lines since the first release), places and school portal (`profile.yaml`) never reach kairo-home. New features are silently denied (default-deny). If a later release raises a `MIN_TIER`, an old file makes `load_gate` refuse to start (`gate.py:190-193`), so a rollback. **H / M**
3. **The release isn't gated on `voice` or `browser`** (`ci.yml:151-156`). Two releases shipped with a red voice job (893f944, 25f2411). It also isn't the zip that was tested: it's rebuilt on Ubuntu (`ci.yml:165-166`). A skipped `windows-install` counts as OK. **M / S**
4. **A manual rollback is undone within about a minute.** The rolled-back version isn't added to `failed` (`Rollback-Kairo.ps1:15-29` vs `Kairo-AutoUpdate.ps1:103-105`). **M / S**
5. **One transient error blacklists a version forever** (GitHub/token/download hiccup → `failed`, `Kairo-AutoUpdate.ps1:141-149`). There's also no newer-than check (104). **M / S**
6. **Update while work is running.** "Ready" only checks the brain lock (`main.py:724`), not the job queue, live calls or the browser. The hard kill leaves `job_runs` stuck in "running" and documents in "working". **M / M**
7. **Migration failure on SQLite can half-apply.** The downgrade then "succeeds" and the backup isn't restored (`Kairo.psm1:640-644`). Downgrades are only tested on an empty DB (`test_migrations.py:32-38`). Likely; needs a check on Windows. **M / M**
8. **Integrity.** The SHA-256 comes from the same release (catches corruption, no signature). pip installs without hashes, as SYSTEM. Voice models have no checksums. **L-M / M**

### D7. Annoyances from real use (top 10)

**The 14-day logs, loops and notifications could not be read** (section 0). Items 1-6 come from your own reports in this session; 7-10 are predicted from the code. Run G.2 and G.4 and read `kairo.log` to replace these with real counts.

1. **Open loops full of codes, notifications, promos, test mails** (your report).
   - #17 filters only the email branch, and #17 isn't released yet.
   - The Task branch still leaks (D1.1).
2. **"Speech recognition failed (500)"** (your report). #16 now shows the real reason; the root cause is UNVERIFIED (the PC's `kairo.log` should have a `Whisper` / `transcribe` traceback).
3. **Mic permission asked on every turn** (your report). Fixed in #16's code; not verified on your iPhone.
4. **Slow call connect** (your report). #16 adds a warm-up; the call turn is still fully serial (D3).
5. **Approvals empty while loops are full** (your question). Cause in A5.
6. **Identical chat replies in the demo recording** (your earlier report). That was the demo brain, not production; a real-path test was added.
7. **Kairo says it will do something and nothing happens.** Expected from A5.1: no tools in production. How the CLI tells the model is UNVERIFIED; check `data\claude-config` transcripts for failed `mcp__kairo__` calls.
8. **The same thing shown 2-3 times:** a mail under "Needs a reply" and "Still open" in the same brief; a test as a deadline loop and a study loop (D2).
9. **Two evening touchpoints 30 minutes apart:** recap mail at 20:30 and check-in at 21:00 (#18, not deployed).
10. **More → Jobs flooded with "skipped (quiet window)" rows**, about 84 a night (D1.12).

### D8. Built per KICKOFF/CHANGELOG but not working, or tested only with fakes

- **Everything that needs a Kairo tool in production:**
  - send/reply via chat (approval cards)
  - tasks, notes, loops and settings by voice or chat
  - debrief, people notes, calendar tags

  The tools aren't shipped (A5.1). The CI tests pass because they run from the repo, which has `mcp_kairo`.
- **Features whose permissions or config never reached the PC** (D6.2):
  - loops/debrief/settings/place/calendar.tag/move/send_self/document tools
  - PA and answer-first personality rules
  - places and the school portal
- **Fakes only, or no test** (from `server/tests`):
  - Google OAuth and every Google call (FakeGoogle)
  - Web Push to the iPhone (stubbed)
  - voice on the GPU (fake `faster_whisper`; CI is CPU with the base model)
  - Syncthing real sync
  - the auto-updater's download, SHA and rollback against GitHub (`-TestRelease` / `-ZipPath` skip them)
  - the leaving-home check (**no test**)
  - OLAT against the real site
  - timetable recurrence against real Google
- **Not built at all:**
  - heartbeat / silent alarm (KICKOFF:304, :752)
  - encrypted offsite backup with restic/rclone (KICKOFF:302)
  - Claude login-expiry warning (KICKOFF 18a-2)
  - catch-up of missed runs (scheduler docstring; only reviews have it)
- **Stale:** the KICKOFF §15 checklist ticks only Milestones A and B; A2, M, C and D are unticked although they're built.

---

## E. Test status

**Against your real account:**
- **None.** No automated test touches real Google, Web Push, Tailscale, OLAT or the iPhone.
- The real Claude CLI is used only by `tests/test_realcli.py` (11) and `test_realcli_owner_mails.py` (5). Both are **skipped unless run by hand** with `KAIRO_REAL_CLI=1`, and they never run in CI.

**Local run** (this audit, `d5ec42f`): **571 passed, 21 skipped**, 383 `def test_` across 48 files. What the 21 skips are:
- 16 real-CLI tests
- 2 browser tests in `test_portal_browser`
- 1 Chromium test in `test_gauntlet`
- 1 real-voice test
- 1 Windows-only DPAPI test

**CI** (`.github/workflows/ci.yml`):

| Job | Runs |
|---|---|
| `server` | Ubuntu + Windows, all tests with fakes |
| `voice` | `test_voice.py` with real Kokoro/Piper/Whisper-base on CPU |
| `browser` | `test_portal*`, `test_gauntlet` with real Chromium against a local fake OLAT |
| `web` | build only; **there are no web/UI tests** |
| `windows-install` | real install/update/rollback/uninstall with a fake CLI; voice and browser skipped |
| `release` | needs server + web + windows-install only |

**CI history** (64 runs, 2026-10-13 12:35Z → 2026-10-14 09:51Z): 41 success, 21 failure, 2 in progress.
- 19 failures were PR iterations.
- 2 were on `main`: the flaky voice test (D1.13), and both were **still released**.

**Skipped in CI:** the 16 real-CLI tests. Also `test_wav_is_valid` in `server` (no numpy), which runs in `voice`.

---

## F. Top 10 fixes (ranked)

1. **Ship `server/hooks` and `server/mcp_kairo` in the release, plus an install test that makes one real tool call.** Without them production has no tools, so no approvals and no voice or chat actions.
2. **Update owner config on upgrade:** repo defaults plus owner overrides, or merge new keys. Today the PC runs first-install permissions, personality and profile, so new features are silently denied.
3. **Stop task-mails from becoming loops and dedupe** (reply check on Task-from-email, one loop per test or mail). This is the main source of loop noise left after #17.
4. **Loop Send only to verified addresses** (real From/To headers, not model output), and show the address before sending. That closes the untrusted-content → send path.
5. **Kill switch blocks approve and edit-approve.** A one-line check on the most dangerous button.
6. **Lag:**
   - stream tokens (`--include-partial-messages`, check it exists in 2.1.296);
   - don't start `mcp_kairo` for tool-less job calls;
   - let chat jump ahead of jobs.

   Chat and calls feel slow mostly because of cold starts and lock waits.
7. **Stop resetting kairo-svc's password on updates, or move the OLAT secret to a store that survives a reset.** Otherwise every update likely breaks OLAT.
8. **Make `kairo.env` and `data\workspace` unwritable for anything SYSTEM or your admin session executes from.** Closes two kairo-svc → SYSTEM/admin paths.
9. **Release pipeline:**
   - gate the release on voice and browser;
   - publish the zip that was tested;
   - make rollback stick;
   - don't blacklist a version on a transient error.

   Stops broken builds reaching the PC within minutes, and stops rollbacks being undone.
10. **Observability:**
    - a push log table;
    - STT, TTS and call-turn timings;
    - first-token time;
    - pushes for Google revoked, Claude logged out, disk low, Tailscale down, missing tools.

    Then the next diagnosis has real numbers instead of "UNVERIFIED".

---

## G. How to get the UNVERIFIED numbers on kairo-home (read-only)

Do this in PowerShell **as admin** on kairo-home. It opens the database **read-only** (`mode=ro`) and changes nothing.

1. Save the script below as `C:\Kairo\diag.py`. That's outside the repo and outside `data\`.
2. Run it:
   ```powershell
   $py = (Get-ChildItem C:\Kairo\app\releases\*\venv\Scripts\python.exe | Sort-Object LastWriteTime | Select-Object -Last 1).FullName
   & $py C:\Kairo\diag.py > C:\Kairo\diag-out.txt
   ```
3. Paste `C:\Kairo\diag-out.txt` to me or to your planner.

```python
import sqlite3, statistics, datetime as dt, json, os, glob
DB = r"C:\Kairo\data\db\kairo.sqlite"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
since = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S")
def q(sql, *a):
    try: return c.execute(sql, a).fetchall()
    except Exception as e: return [("ERROR", str(e))]
def t(x): return dt.datetime.fromisoformat(str(x).replace("Z", "")[:26]) if x else None
def pct(v, p): v = sorted(v); return round(v[min(len(v)-1, int(len(v)*p))], 1) if v else None
print("== G.1 approvals by status (14 d), and how many came from loop Send")
print(q("SELECT status, COUNT(*), SUM(reason LIKE 'Open loop%') FROM approvals WHERE created_at>=? GROUP BY status", since))
print("== G.2 open loops created (14 d) by kind/source/status; close reasons")
print(q("SELECT kind, source, status, COUNT(*) FROM open_loops WHERE created_at>=? GROUP BY 1,2,3 ORDER BY 4 DESC", since))
print(q("SELECT close_reason, COUNT(*) FROM open_loops WHERE closed_at>=? GROUP BY 1", since))
print(q("SELECT kind, who, substr(what,1,70), raised, snoozed FROM open_loops WHERE status='open' ORDER BY raised DESC LIMIT 25"))
print("== G.3 job runtimes (14 d): job, status, n, p50 s, p95 s")
rows = q("SELECT job, status, started_at, finished_at FROM job_runs WHERE started_at>=?", since)
agg = {}
for job, st, a, b in rows:
    if a and b: agg.setdefault((job, st), []).append((t(b) - t(a)).total_seconds())
for k, v in sorted(agg.items()): print(k, len(v), pct(v, .5), pct(v, .95))
print(q("SELECT job, substr(error,1,120), COUNT(*) FROM job_runs WHERE started_at>=? AND status='error' GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20", since))
print("== G.4 pushes we can count (14 d)")
print("loop/leaving pings per day", q("SELECT substr(ts,1,10), COUNT(*) FROM audit_log WHERE action='proactive.ping' AND ts>=? GROUP BY 1", since))
print("reviews", q("SELECT kind, COUNT(*) FROM reviews WHERE created_at>=? GROUP BY 1", since))
print("calls", q("SELECT reason, status, COUNT(*) FROM calls WHERE created_at>=? GROUP BY 1,2", since))
print("briefs ok", q("SELECT COUNT(*) FROM job_runs WHERE job='morning_brief' AND status='ok' AND started_at>=?", since))
print("suspicious mails", q("SELECT COUNT(*) FROM email_triage WHERE suspicious=1 AND triaged_at>=?", since))
print("captures asking", q("SELECT COUNT(*) FROM captures WHERE status='asking' AND created_at>=?", since))
print("== G.5 model runs (14 d): purpose, model, n, failed, p50 ms, p95 ms, tokens, usd")
rows = q("SELECT purpose, model, ok, duration_ms, COALESCE(input_tokens,0)+COALESCE(output_tokens,0), COALESCE(cost_usd,0) FROM model_runs WHERE ts>=?", since)
agg = {}
for p, m, ok, d, tok, usd in rows:
    a = agg.setdefault((p, m), {"n": 0, "fail": 0, "d": [], "tok": 0, "usd": 0.0})
    a["n"] += 1; a["fail"] += (not ok); a["tok"] += tok; a["usd"] += usd
    if d: a["d"].append(d)
for k, a in sorted(agg.items(), key=lambda x: -x[1]["n"]):
    print(k, a["n"], a["fail"], pct(a["d"], .5), pct(a["d"], .95), a["tok"], round(a["usd"], 2))
print("== G.5b chat wall time (owner message -> Kairo reply, same thread), s; calls are lines starting '(spoken'")
msgs = q("SELECT thread_id, role, text, created_at FROM chat_messages WHERE created_at>=? ORDER BY id", since)
chat, call, last = [], [], {}
for tid, role, text, ts in msgs:
    if role == "owner": last[tid] = (t(ts), (text or "").startswith("(spoken"))
    elif role == "kairo" and tid in last:
        s0, spoken = last.pop(tid); (call if spoken else chat).append((t(ts) - s0).total_seconds())
print("chat n/p50/p95", len(chat), pct(chat, .5), pct(chat, .95), "| call n/p50/p95", len(call), pct(call, .5), pct(call, .95))
print("== G.5c voice notes: upload -> filed, s")
vn = [(t(b) - t(a)).total_seconds() for a, b in q("SELECT created_at, filed_at FROM captures WHERE source IN ('voice_note','app_voice') AND filed_at IS NOT NULL AND created_at>=?", since)]
print(len(vn), pct(vn, .5), pct(vn, .95))
print("== G.6 installed releases and update state")
print(sorted(os.path.basename(p) for p in glob.glob(r"C:\Kairo\app\releases\*")))
for f in glob.glob(r"C:\Kairo\data\*update*.json") + glob.glob(r"C:\Kairo\updater\*.json"):
    print(f, open(f, encoding="utf-8", errors="replace").read()[:600])
```

Logs to read on kairo-home (text, not in the DB):
- **`C:\Kairo\data\logs\kairo.log`** (5 files × 5 MB, `serve.py:37-49`). Search it for:
  - `quiet hours: no push for` (dropped pushes)
  - `push failed`
  - `gave up waiting` (job queue)
  - `maximum number of running instances` (APScheduler drops)
  - `Whisper` (voice load)
  - `usage_limited` and `logged_out` (Claude)
  - `GoogleNotConnected`
- **Updater and boot-check logs:** see B1 for paths.
- **STT and TTS time per request:** not stored anywhere (section C). Measuring them needs a code change, which wasn't made (diagnosis only).

