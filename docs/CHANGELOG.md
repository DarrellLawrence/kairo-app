# Changelog

## 2026-10-23 — build: speech to text on calls, the Whisper part of the voice plan (group G2)
- Calls write your words down with fewer surprises. Four fixes are on from the next update, each with a switch in More → System → Experiments (the names start with `g2.`): `g2.temperature_zero` (on): a sentence is decoded once, never again with random variation when Whisper is unsure (its default list goes up to 1.0 and can run the decode up to six times, with different words each time). `g2.offline_models` (on): Whisper loads only the model files the installer downloaded and never goes online to look for them; a model that is not there is reported ("run Update Kairo"), not fetched inside the first sentence of a call (that was a 1.5 GB download in the middle of a call). With only the small model there, the call runs on the CPU and Voice speed says why. `g2.warm_at_connect` (on): when a call or the voice button connects, Whisper writes down one short test sentence in English and one in German in the background (made by Kairo's own voices), so your first real sentence does not meet a cold graphics card; start-up warms it the same way (it used to warm on a second of noise with the voice filter off). It is skipped when the card has just worked or when you are already talking. The switches are read once when a call connects.
- New: Language on calls can be German only (More → Settings → Voice). With English only or German only, Whisper decodes in that language at once. "English and German" stays the default and still lets Whisper find the language.
- Off until the PC has measured: `g2.single_encode_langid` (off): in "English and German" Whisper encodes the audio twice (once to find the language among 99, once to write); with this switch it encodes once, reads English or German from that same pass, writes the words from it, and takes the language only when English and German together carry at least half the probability (else the last turn's language, so a one-second clip heard as Dutch does not turn the answer Dutch). If it ever fails, the call goes back to Whisper's own finding and Voice speed says so. `g2.int8_float16` (off), or `KAIRO_WHISPER_COMPUTE=int8_float16` in kairo.env: the lighter number format on the graphics card, about 0.8 GB less graphics memory at the same speed; a GPU that cannot do it keeps float16 on the GPU (never drops to the small CPU model) and says why. Changing it loads the model again at the next call.
- Voice speed and `kairo diag` now show which compute type Whisper really runs in (`float16`, `int8_float16`, `int8_float32` on the CPU) and the ctranslate2 version; the latency harness records the same instead of a guess.
- The self-test that Update Kairo runs now also has the German voice say a sentence and Whisper write it down again, the way a German call does (German given). What Whisper heard is reported in the result (`heard_de`, `german_ok`, `notes`); a wrong word never sends an update back, because a small model mishearing one sentence is no sign of a broken update.
- Numbers (sandbox CPU, not the PC; Whisper small int8, 4 shared cores, 21 synthetic clips of 0.7 to 9 s, median of 3): language given 2.32 s, language=None 4.18 s (1.80 times), one pass 2.35 s (1.01 times); encoder passes 2 against 1; same words as with the language given on 20 of 21 clips; language right 19 of 21 (language=None: 18 of 21). The card's saving, the cold-card cost and the word-error rate of int8_float16 on your voice can only be measured on the PC: P2, P24 and P25 of the voice plan's PC list (speed: fixed against auto and fp16 against int8_float16; word errors on about 4,200 words of your own; language finding on short clips of yours).
- To do by hand: nothing for the four fixes. When you want the other two: run P2 (and P24, P25) on kairo-home, then switch them on in More → System → Experiments; the first call after switching loads the model again (a few seconds, in the background). The plan also asks you to pin `ctranslate2==4.8.3` in requirements-voice.txt (it is unpinned, so the PC may run any 4.x); that is your decision (a pinned dependency), nothing was changed there. The single-pass path is pinned to faster-whisper 1.2.1 and a test fails if that version is bumped.
- Found while building it: asking `transcribe_pcm` for "auto" used to hand Whisper the word "auto" as a language (nothing did, but the call path now treats it as "find it"); the diagnosis code asked for the model folder through a call that fell back to the bare model name.
- Removed: the `"en" if … else None` language line in voicertc.py and the same line for the old line in main.py (now English, German or find it); the fixed `compute_type="float16"` in `_make_stt`; the bare-model-name fallback of `_local_model` (a download inside a call; `Voice.model_folder` is the lookup that never raises, and `kairo diag` uses it); the start-up warm-up on noise with the voice filter off; the guessed compute type in the latency harness's config.

## 2026-10-23 — build: the voice latency harness (owner's spec)
- New: a harness that times the call the way you hear it, on the real pipeline: `python -m app.bench_voice` (in server/). A fake phone (aiortc) calls Kairo's own /api/rtc/offer like the iPhone, plays 46 short English and German lines at real time (questions, statements, requests that only read, "mhm" and "okay", noises, other people's voices) and measures from the exact end of each line to the first audible reply frame. Per turn it adds the steps from the server's own timing for that turn (the VAD pause, the turn decision, Whisper, the first word, the first audio) and what is left as the network.
- Rates: does a backchannel or a noise stop Kairo while it talks (it must have been heard by the PC's VAD and the voice must go silent), how many real questions got no reply in 5 s, how often the speaker check lets a stranger's voice through (not known when the check is off).
- One JSON per run in data\bench\results (the fields and how to read them: bench/results/README.md); pass = p95 within 1.0 s, no missed turn, at most 5 % false interrupts. `run(mock, url, out_dir)` and `latest(results_dir)` are there for `kairo diag`.
- The lines (bench/voice/corpus.json) are made once with Kairo's own voices (`python -m app.bench_corpus render`: several Kokoro voices and speeds, Piper for German, noises from recipes) into data\bench\corpus, each with its exact end of speech. Your own recordings in data\bench\own\<line id>.wav win. No audio is in the repo; the list ships in the release (the release check fails without it).
- `--mock`: the same client against the real Pipecat pipeline (Silero VAD, Smart Turn) with stand-in speech to text, voice model and voice of known speed and speech-like audio made on the spot: no models, no GPU, no Kairo. CI runs it on Linux and Windows and checks that every stage is found within 45 ms of the wait built into it, also with other waits.
- Safe by design: it calls only http://127.0.0.1 or Kairo's own .ts.net address; on the PC itself it is the owner already (the existing local-caller rule: no token, nothing new, a test pins it down); no STUN server; it won't start while a call is under way, because a new call replaces the old one; the lines only read (calendar, to-do list, weather), so a live run changes nothing of yours. Its calls appear in the chat history like any call.
- Voice.speak takes an optional voice and speed (the renderer's); calls are unchanged.
- Found while building it (CPU only, not the PC): the PC's VAD calls the end of speech 85 to 145 ms after its 0.2 s pause and the audio path adds about 100 ms, both land in the network step; a short "yeah" or "genau" played over Kairo stops its voice (the VAD hears speech and Pipecat cuts the answer); a line with a pause of more than 0.2 s in it is heard as two pieces, and Smart Turn can call the first one a finished sentence and answer it alone.
- Removed: nothing. Nothing existing is replaced: it reuses turns.steps, latency.pct, voicelab's helpers, the speaker check's voiced-frame rule and the fake-phone approach of test_voice_rtc.

## 2026-10-23 — build: `kairo diag` and the Voice check page (your spec: "kairo diag exits 0")
- New command on the PC: open a Command Prompt or PowerShell and type `kairo diag`. One line per check, PASS / WARN / FAIL, with the number where there is one and the reason in plain words when something fails. It exits 0 when nothing fails, 1 when something does, 2 when the tool itself broke. A WARN doesn't fail it (`kairo diag --strict` counts warnings too). `--json` prints the same report as data, `--deep` also checks the checksums of the multi-GB model files (slow), `--local` doesn't ask the running Kairo.
- The same checks are in the app: More → System → Voice check (the address is /diag/voice). It shows the verdict, every check (the failing parts first), "Run again", and the newest live run of the latency harness: p50 / p90 / p95 as bars against the 1.0 s target, one bar per step, the three error rates, the machine and the settings.
- What it checks: for Whisper, Kokoro, Piper, the voice model (llama.cpp), Smart Turn, Silero VAD, the speaker model and the memory embeddings: installed (the speaker model against the size and sha256 pinned in voicellm.json, the voice model's size and the installed llama.cpp version against what the release pins), loaded and warm, and on the GPU where it should be. A part on the CPU that should be on the GPU is a FAIL with the reason. Also: graphics memory against 12 GB and main memory against 16 GB (WARN from 85 %, FAIL from 95 %), the voice model answering on 127.0.0.1 with the context size, slot count and chat template it was started with, HF_HUB_OFFLINE in Kairo's own environment (WARN), your voice enrolled (WARN until you taught Kairo), the Windows Firewall rule for calls (on, UDP only, Tailscale's addresses only, for Kairo's Python), direct or relayed to your phone, and WebRTC or the old line.
- The latency harness (server/app/bench_voice.py, built separately): when it is there, diag runs it in mock mode as a self-check (into a temporary folder) and reports the newest live run's p50 / p90 / p95 and pass; FAIL when the run missed the target, WARN when there is no live run yet, when it is older than 14 days or was made on another version. Until the harness is merged the two lines say "harness not installed" (WARN).
- It only reads. Kairo's server builds the report from its own state (`GET /api/diag/voice`, owner only like every route, secrets and addresses redacted, nothing written into the data folder); the command asks that server on 127.0.0.1 and, when none answers, judges the files it can see and says so on the first line.
- Found while building it: calls over WebRTC use Pipecat's own copy of the Smart Turn model (inside its package), not the file the installer downloads into the voice-model folder; that copy only serves the old line and the voice lab. Smart Turn and Silero are loaded per call, not kept warm; diag proves each model loads and answers (a few ms) instead.
- To do by hand: nothing. After the next update, open a new Command Prompt (an open one doesn't know the new PATH entry) and type `kairo diag`. The Voice check page needs no setup. Two things are for your OK because they touch rules in CLAUDE.md: (1) `diag.py` is a new line in server/app/outbound.MODULES: its one call is a GET to Kairo's own server on 127.0.0.1 (host fixed in code, no proxy); (2) the installer adds `C:\Kairo\bin` to the machine PATH. Only administrators can write to that folder (Kairo's own account has read and run rights only, and the Windows CI checks it), so nothing Kairo runs can put a program there. Uninstall removes both.
- The firewall line: if Kairo's own account isn't allowed to read Windows Firewall, the app shows a WARN for it and `kairo diag` (run by you, as an administrator) judges the rule itself.
- Removed: nothing existing was replaced; the checks read what `Voice`, `gpu.py`, `voicellm.py`, `tailnet.py` and `diagnosis.redact` already know instead of repeating it. Added `voice_local.props()` (llama-server's own settings) next to `health()`.

## 2026-10-23 — fix: the last holes in the privacy guard rails (second adversarial review)
- Commit messages: the hook checks every line git keeps. Text below a `# ---- >8 ----` line in `git commit -m`/`-F`, `#` lines in `git merge -m`, and every line you type in the editor (whatever it starts with) are checked; only git's own comment block (and the diff of `commit -v`) is left out, and only while it is unchanged. One gap: `--cleanup=whitespace` typed on the command line (set `commit.cleanup` in the git config instead); CI checks every message anyway.
- Releases: the release-zip check runs with `--ci` again, so without the `PERSONAL_TERMS` secret nothing is released (commit f1b8ca9 had switched that off, and a zip could ship unchecked). A pull request from this repository is red when the secret is missing, so you see it before the merge.
- A pull request can no longer quiet gitleaks: a `.gitleaksignore` file and gitleaks' inline allow comment (outside the test, docs and installer/ci folders) are findings, and CI deletes the ignore file before gitleaks runs. Every CI job gets a read-only token unless it asks for more (only `release` does); the build job keeps no git credentials while npm runs.
- The scanner now also finds: a term with any invisible character inside it (format characters, combining marks, control characters), UTF-32 text without a BOM, every part of a commit header (the `encoding` value, header names), base64 right after `=` and base64 wrapped over lines (mails), every stream of a multi-stream `.bz2`/`.xz` (pbzip2 dumps) and data appended after it, raw zlib data, a VAPID private key listed after its public key (gitleaks too) and the output of `web-push generate-vapid-keys`. lz4 and lzip files are "not scanned" instead of passing as bytes.
- A long git error is checked for your terms before it is cut; a name or a message with a term glued to other letters is hidden too.
- To do by you: (1) add the `PERSONAL_TERMS` secret before you merge this; without it this pull request and `main` are red and nothing is released. (2) Merge by hand any pull request that touches `.github/`, `scripts/privacy_scan.py`, `scripts/hooks/`, `.gitleaks.toml` or `scripts/build-release.sh`: its workflow comes from the pull request itself, so only your review stops it from skipping the checks or reading the secret. CLAUDE.md still lets Claude merge its own pull requests on green CI; tell Claude if this exception should be written there. (3) If you installed the hooks with `--install-hooks`, run it again (the commit-msg hook changed).
- Removed: `--message-editor` (now `--message-prepared`; hooks older than the scanner get a "reinstall the hooks" error), the `kairo-msg-source` note of the hooks, the hand-written list of invisible characters, the release-zip check without `--ci` and the test that locked it in, and the 200-character cut of git errors before the term check.

## 2026-10-22 — build: personal data and secrets can't get into the repo or a release again (your prompt 0.6)
- New `scripts/privacy_scan.py` (Python only, runs on Windows and Linux). It checks the lines a commit adds, a folder, the whole git history (files, commit objects with all their headers, commit messages, author and committer, branch and tag names) and the release zip. It looks for your personal terms (a list that lives only on your PC), for secrets (key blocks, Google, GitHub, Tailscale, OpenAI and AWS keys, VAPID and JWK private keys, long random passwords, tokens and `*_KEY` values) and for paths that must never be committed (`private/`, `secrets/`, `vault/`, `data/`, `devdata/`, `*.local.*`, `.env` and `*.env`, databases, documents and photos outside `web/public/`).
- Your terms are never printed, not even in the CI log or in an error message: a hit shows `file:line` and the rule number (`personal term #4 (list line 7)`), a name that holds a term shows as `<redacted name #3>`. Matching ignores case, folds umlauts (u-umlaut = `ue` = `u`, sharp s = `ss`), never fires inside a longer word, finds `first last`, `first.last`, `FirstLast`, `firstlast@...` and `last, first`, matches a number with any grouping (`079 555 12 34` = `0795551234` = `+41 79 555 12 34`), and a line `regex:...` is a regular expression. Text is read after decoding `\u00fc`, `\xFC`, `%C3%BC`, `&uuml;`, `=C3=BC`, mail headers and base64.
- It reads what it used to skip: PNG/WAV/MP3/EXIF text, PDFs (metadata and compressed text), UTF-16/32 files, zip, gzip, bzip2, xz and tar files inside each other (also a zip behind an installer stub), zip comments and symlinks. Anything it cannot read (7z, rar, zstd, encrypted or damaged members, too deep) is "not scanned": a warning on your machine, an error in CI and for a release zip. No 32 MB limit any more: big files are read in pieces.
- Where the list is: `C:\Kairo\data\config\personal-terms.txt` on the PC (Kairo does not copy it into its effective config), `~/.config/kairo/personal-terms.txt` on the laptop, or the GitHub secret `PERSONAL_TERMS` in CI. Save it as UTF-8, one term per line (a Windows-1252 or UTF-16 file from PowerShell is read too, with a warning; a list on one line, or with a comma, is an error). A list you named (`--terms-file`, `KAIRO_PERSONAL_TERMS_FILE`) that does not exist is an error. Without a list the check is skipped with a warning; in CI on `main` that is a red build, so you notice the missing secret.
- Git hooks: `git config core.hooksPath scripts/hooks` (once per clone, in README and CONTRIBUTING.md), or `python scripts/privacy_scan.py --install-hooks` to also protect old branches and every worktree. `pre-commit` scans what you are about to commit (plus gitleaks with `--verbose` if installed), `commit-msg` scans every line of the message (also lines that start with `#`; git's own comment block is ignored only when an editor was opened).
- CI: job `privacy` scans the new commits of a pull request (hard) and the whole history (hard on `main`; for a pull request it only reports, because the old history still holds the `private/` file until the history is rewritten) with the scanner and with a pinned gitleaks (checksum verified, looks 3 archives deep, never `--verbose`, so no term leaks into the log). A pull request is judged by the scanner and gitleaks config of its base branch, not its own. New job `privacy-zip` scans the exact release zip with your terms and unpacks it for gitleaks, and **`release` now waits for it**: until `PERSONAL_TERMS` exists, `main` is red and no release is published. The build job no longer receives the secret. `scripts/build-release.sh` deletes a zip that fails the scan.
- `.gitignore` also keeps out `data/`, `secrets/`, `vault/`, `.private/`, `*.local.*`, `.env.*` (except `.env.example`), `*.env`, `.envrc`, `.env-*`, `*.sqlite*`, `*.db`, `*.db3`, `capture-token*` and the terms list. `.gitleaks.toml` has a rule for VAPID private keys.
- To do by you: add the GitHub secret `PERSONAL_TERMS` (one term per line, real line breaks) and put the same list, saved as UTF-8, in the two files above. Until the git history is rewritten (separate step, only on your word) the `privacy` job on `main` reports the old `private/` file that is still in it, and with your terms set it reports every place they still are.
- Removed: the 32 MB skip, the list of file types that were never read (images, `.pdf`, `.gz` ...), the line-dropping of `#` in the commit-msg hook, `gitleaks --verbose` in CI, the terms secret in the build job, the hash-of-the-name label of hidden names, the `git log` text format that broke on control characters, and the old test that expected a missing list file to be ignored.

## 2026-10-20 — build: calls sound like a person (owner's calls on the iPhone)
- After "Hello" a call said `<|tool_call_start|>[ask_kairo()]<|tool_call_end|>` out loud: the voice model writes its tool calls as text and the PC passed them on as words. Now they are taken out before anything is said (never even half), read as real calls, and a call is only run when Kairo offered that tool on the turn and it has what it needs; anything else is dropped without a word. "Hello" gets "Hey <your first name>." at once, without the voice model; a reply that was only a dropped call still says a short line ("Okay.", or "Sorry, can you say that again?").
- Claude's notes are no longer read out ("The owner mentioned…", "L23 (urgent, loop)", "Let me check the bring-up list", numbered lists). Claude is asked for plain sentences to you, and everything a call says or shows goes through one clean-up as a safety net: ids and tags out, thinking out loud dropped, a list becomes at most two short sentences. The chat keeps what Claude wrote.
- Bring-ups: at most one a call, only after you asked or said something real (never after "hello", "thanks", small talk or noise), and never both said on the call and pushed: one pushed in the last 6 hours isn't said, and one said on the call isn't pushed as well.
- Acceptance test 43: call answers are plain speech (automated, with the real Claude). Acceptance 39 and 41 now say one bring-up a call.
- Removed: the "two bring-ups a call, urgent not counted" cap.
- Past deadlines read "overdue since Fri" everywhere (Today, the card the voice reads, the brief, the recap, calls), never "due Fri"; snoozing an overdue item works again.
- Personal to-dos come back in the evening at home (17:30 on, weekends too), never on the commute or during a call, each with one concrete next step; every skip brings the next one sooner and more directly, after 3 a smaller first step.
- The orange line on the call screen only shows when something is wrong, not because the local voice is your choice.
- Setup: the iPhone sync step says where the Device ID and the address go in Mobius Sync.
- On calls Kairo answers only you (your approval 2026-10-20: one model file, no new package). A turn much quieter than your own voice on that call (the TV, someone across the room) is ignored. Once you teach Kairo your voice (More → Settings → Voice → Teach Kairo your voice: read a short text for 30 s), other voices are ignored too: no reply, no Claude, nothing brought up, the call keeps listening. Short words like "yes" are only ignored when clearly someone else's. The voice button is always answered.
- Speaker check: strict / normal / off (default normal). The model is WeSpeaker CAM++ (29 MB), downloaded and checked with the voice models (pinned in voicellm.json); without it the check says "model missing" and is off, calls still work. It runs next to Whisper on the GPU, so a turn waits a few ms at most. Your voiceprint stays on the PC, encrypted; the recording is not kept; Remove deletes it.
- Voice speed lists ignored turns ("ignored: not you", "ignored: too quiet / far") with time and score, never the words, and the speaker check's device and time per turn; Run diagnosis has the counts. Acceptance test 43.
- Found while building it: ONNX Runtime 1.24 (the PC's version) computes this model wrong with its graph optimizations on. Kairo checks every way of running it against the plain one when it loads and uses the first that agrees.
- Updates: a voice model file the previous voice-model folder already has (same checksum) is copied, not downloaded again.
- Check-ins don't stack up any more: one open per kind (daily, weekly…); a newer one closes the older as missed (it still counts against the streak), a daily one expires at the end of its next day, and the pile already on the PC is cleaned up once.
- Mail a machine sent (no-reply / notification / newsletter senders, mailing lists, auto-replies, what triage calls automatic) never causes a push: not as "suspicious mail", not as a reply loop, not in the brief. It is still labelled and listed. Mail from people notifies as before. A suspicious mail from a machine is labelled Kairo/Suspicious in Gmail, without a push.
- The morning brief now sees the open loops (people waiting, promises), so an urgent one can make the top 3.
- Calls: tap the orb when you're done talking and the turn ends at once (no wait for a pause; for loud rooms). The speaker check still runs on it; Voice speed shows "ended by tap".

## 2026-10-19 — build: your profile only in the vault (your decision)
- Your profile file is out of the repo and out of the release. Kairo imported it once on your PC (Run diagnosis showed it); it lives only in the vault there (10 Profile/), which every chat reads. `private/` is ignored by git, and a test fails if it ever comes back.
- On the next update your PC also deletes the copy inside the older Kairo versions it keeps for going back.
- Removed: the one-time import from the release (Kairo's start no longer looks for the file) and its CI switch. To change your profile now: send the file in chat and say "save this to my profile"; only that reaches the people, coach and calendar settings, editing the vault copy by hand doesn't.
- Still on GitHub: the git history and the releases published while the file was in them. Deleting those releases is listed in the PR; the history needs a rewrite plus GitHub Support, only if you ask for it.

## 2026-10-19 — build: the update really switches; the voice back on the GPU (owner's diagnosis)
- Found: your PC said "Kairo updated to e9f997a, ok" but kept running 7a7a554. The new version started (its voice warm-up is in the log), but the old server still held Kairo's port, so the health check and the self-test talked to the old one. Now the installer stops every Kairo server, also one the task no longer knows (found by its command line or by the port), never restarts the old code before the switch, and only counts an update when /health reports the new version; another version answering is stopped once, and if it's still not the new one, the update is undone and pushed. e9f997a was the newest release; there was nothing newer.
- New: if the installed version isn't the one running for 10 minutes, that's a red chip on Today and a push. Run diagnosis shows installed vs running.
- Found: "cublas64_12.dll not found" (Whisper) and "CUDA/cuDNN DLLs not found" (Kokoro) came from my change of 17 Oct: the second pip run (the GPU add-on) replaced the whole nvidia folder and deleted cuBLAS and cuDNN. All NVIDIA packages are installed in one run now; CI checks that every DLL is there and loads.
- The self-test is red (and pushed) when Whisper, Kokoro, the voice model or the embeddings run on the CPU on your PC; it never undoes an update for that. The embeddings go back on the GPU when 3 GB are free. Voice speed lists the embeddings too.
- Removed: "the voice model wins the VRAM" (embeddings always on the CPU).
- The voice lab never ran because it gave up after 3 skipped tries, stopped silently on an error, and waited forever without saying why. Now it runs by itself as soon as the voice model is ready and every voice part is on the graphics card, picks the model, and keeps checking until it has a run on the GPU. Voice lab and Run diagnosis show what it is waiting for. A run on the CPU never switches the local voice off.
- Open loops: a shop's mail ("is everything fine with your order?") never becomes a loop or a task any more, on any path; the same thing from the same sender is one loop. The 3 duplicate loops on your PC close by themselves after the update (as automated mail), and leftover duplicates are merged once.
- Calls used Sonnet and Opus because the request Kairo wraps around every ask_kairo ("Answer in one to three sentences…") matched the pattern meant for "answer to <someone>", so every ask went to Sonnet. Now: ask_kairo and call reports run on Haiku, also for adding tasks and reminders; only a real judgment call goes to Sonnet, and More → Usage lists every run that isn't Haiku with the reason. Jobs never fall back to the CLI's default model any more.
- Acceptance fixes: #1 "add task … tomorrow" failed because a date without a time crashed saving the task (now noon your time); a failing tool now says what went wrong instead of "backend unavailable", and the database waits for a busy writer (WAL mode: kairo.sqlite-wal and -shm next to the database are normal; copy the database only with the backup tools). #10 the brief is at most 7 short lines. #19 "leave it" means the thing talked about last. #21 Kairo answers in the language of your message. #30 the tailnet address now comes from the auto-update every 5 minutes, not only from the boot check.
- Your profile (your decision 2026-10-19): it's in the private repo (private/profile/ABOUT_ME.md) and ships with the release. On the first start after this update Kairo imports it by itself, like "save this to my profile": as-is in 10 Profile, people and birthdays, to-dos, calendar rules (training FIXED; weekends SOCIAL as a weekday rule, a training on a weekend stays FIXED), coach facts, places and routes, goals in 20 Goals, and school days as weekly FIXED blocks in your calendar. Then one push: what was set up, and what's unclear as quick questions. If you change the file later, only the changed sections are read again. Its text is never printed anywhere (logs, CI, tests, PR text); a PC with another owner's name and CI's test installs never import it. Run diagnosis shows the proof in counts: goals, coach settings, timetable blocks, people, calendar rules, open questions.

## 2026-10-17 — build: failed updates always push; a slow voice says so (owner's rules)
- Every update that fails now sends a push with the reason, every time: each failed try (with the try number and when the next one comes), an update that was undone (with the check that failed), a new version still waiting after 3 hours outside the quiet hours (and why: a chat, a job, a call), an error in the auto-update that lasts 30 minutes (then every 6 hours), and a missing GitHub token. Before, only an undone update and the 3rd failed try pushed; errors and endless waiting were silent.
- Kairo also watches the auto-update itself: if it hasn't run for 30 minutes (its task is off or it breaks before it starts), that's a push and a red chip on Today.
- A broken auto-update memory file no longer stops all updates: it is set aside and the auto-update starts fresh.
- Update pushes are no longer cut at 200 characters, so the reason fits.
- A voice part off the graphics card no longer goes unnoticed on a call: the call goes ahead, Kairo says once at its start "Heads up: my voice is slow right now, there's a GPU problem. I've sent you the details." and pushes which part and why. Same check as the push when Kairo starts.
- Removed: the "only on the 3rd try" rule for update pushes.

## 2026-10-17 — build: voice on the GPU (owner's report from the iPhone)
- Found: the PC never got the last two updates (still 7a7a554). And a bug that very likely put Whisper on the processor since 14 Oct: it looked for its CUDA files only in a folder named "voice", but the voice packages moved to "voice-<number>". Fixed: it looks in the real folder. Voice speed now shows whether that was it.
- Kokoro (the English voice) runs on the graphics card too (onnxruntime-gpu, your approval 2026-10-17). Whisper and Kokoro load once when Kairo starts and run once to warm up; no turn waits for a model.
- Voice speed, on top: which line your last call used (WebRTC or the old line, and why) and where each part runs: Whisper, Kokoro, the voice model (layers on the GPU), the German voice, and the graphics card's memory. Anything on the processor is red with the reason. Kairo also sends one push when it starts with the voice on the processor. Run diagnosis lists the same.
- On a call: "hello", "thanks", "bye" are answered by the voice model without Claude (no tool offered at all). Claude gets 8 seconds; then Kairo says "I'll get back to you." once and the call goes on; the answer comes as a push and in your next turn. The holding line at most once a turn; no more "still on it".
- A call you place opens with "Hey <your first name>." at once (made once, played from the cache).
- The old line also starts the voice after the first clause or 5 words.
- Updates: the voice folder is now built in CI exactly as the installer builds it on the PC, then checked; releases wait for it. The auto-update notes every 5 minutes what it is waiting for and which versions failed (C:\Kairo\status\updater.json, in Run diagnosis).
- Acceptance test 42: voice on the GPU.
- Removed: "Still on it." and the first-sentence-only cut on the old line.

## 2026-10-16 — build: Voice V3, speed (owner's request)
- Calls and the voice button now go over WebRTC, built on Pipecat: your iPhone sends its mic straight to the PC over Tailscale (Opus, the iPhone's echo cancellation) and plays Kairo's voice from the same connection. Talking over Kairo stops it at once, and it doesn't hear itself.
- The PC decides when you're done: a 0.2 s pause, then Smart Turn v3 (a turn model) checks whether the thought is complete; if not, it waits up to 0.5 s more. Whisper writes your words down at that first pause, so the text is ready when the turn ends.
- The voice model starts answering at the pause, before the turn is confirmed; if you go on talking, that start is thrown away. It never calls Claude early.
- Kairo's voice starts after the first clause (about 5 words), not the whole sentence; short openers and holding lines ("Sure,", "One sec.") are made once and play at once.
- Voice speed: each turn's steps over WebRTC (turn decided, first word, first audio, network + playout), target p50 0.65 s / p95 1.0 s, every turn over 1 s with its slowest step, and whether your phone reaches the PC directly or through Tailscale's relay, with the round trip. Run diagnosis has the same.
- On the PC: the installer lets the call's audio in to Kairo's Python from Tailscale addresses only; the auto-update notes every 5 minutes which devices reach the PC directly or relayed (no public addresses kept).
- Acceptance 3, 4, 15 and 39 now require p50 ≤ 0.65 s and p95 ≤ 1.0 s on the iPhone, on Wi-Fi and on mobile data.
- If WebRTC can't connect (or the voice packages aren't there), the phone uses the old line by itself.
- Removed: the 0.8 s / 1.5 s voice targets; on the WebRTC line, the phone deciding when a turn ends (the PC decides); the 1.0 s wait after "not done" (now 0.5 s); av 18.1 (17.1: WebRTC needs it).

## 2026-10-16 — build: voice answers (owner's decisions)
- If no voice model passes the voice lab, calls use the old voice until you pick a model yourself (Voice lab → Use this one). Before the lab has run, the smallest one still talks.
- The voice lab asks 20 Today-card questions per language (weather, trains, what's next, school, tasks, loops) instead of 3, so one wrong answer no longer decides it.
- Your answer to something Kairo brings up goes to Claude during the call, not after it: together with your next question when that goes to Claude anyway (no extra wait), else right after Kairo's reply. A later question on the same call gets the new state.
- Your places come from your profile: share it in chat with "save this to my profile" and Kairo saves home, school, work and training with their stations, the weekdays each applies (school Mon/Tue, work Wed-Fri) and the station your usual route goes via. The Today card's next connection uses them: the right destination for the weekday, through your usual route. Undo puts the old places back. You can still set them by voice ("my school is on Mondays and Tuesdays").
- Fixed: the English voice sometimes said nothing or made noise. The small (int8) Kokoro model Kairo used went wrong on ordinary sentences ("Sure, I will remind you.": no sound; others far too loud). Kairo now uses the fp16 model (177 MB, downloaded once with the update) — no broken sentences in the tests and about 5x faster on the processor. A sentence the voice model still gets wrong is reported as an error, never sent as silence or noise.
- Removed: the int8 Kokoro model; "your answer is recorded after the call" (it's live now; after the call only what a cut-off turn left).

## 2026-10-16 — build: Today card and using the wait (owner's request)
- Today card: the voice model on the PC answers the usual questions at once, without Claude: what's next (the next 3 events), what's left today (tasks due today or overdue), open loops, the weather at home, the next train or bus for your routine (from your saved places: home station to where you're heading, or back home), key profile facts and what Kairo remembers lately.
- Always fresh: each part on its own clock (Kairo's own data every minute, the calendar every 3 minutes, the connection every 3 minutes on a call and every 10 otherwise, not at night, the weather every 30), and read again at once after Claude changed something on the call. A call only gets the parts that changed, so the voice model stays fast. Only station names leave the PC, as before.
- Only what the card can't answer goes to Claude. Voice lab: each model's share of card questions answered on the PC with the right facts (a model needs 70%, at most 10% wrong, to be picked). Voice lab and Run diagnosis: how many of your call questions were answered on the PC vs via Claude.
- Using the wait: while Claude works, Kairo says a short holding line and then one thing from its list for you, in Claude's words ("Checking. Quick one: did you answer Lea about Friday?"): the one that fits what you're talking about, the time and the place. Never the same one twice in a call, at most two a call; nothing worth it: just the holding line.
- The answer never waits for it: a normal item is only said while Claude is still working; Claude's answer comes right after the sentence that is playing. An urgent item is said first, before the answer.
- What you answer to it goes to Claude after the call (done: closed; later: snoozed; new information: saved), unless the voice model already passed it on. Talk over Kairo in the wait and the answer still comes, right after Kairo answers what you just said.
- Claude now writes each call topic as one short spoken sentence in English and German.
- The owner's words carry the time on calls, so "in 10 minutes" stays right during a long call. A long call that nears the voice model's memory starts a fresh conversation with the last lines.
- Nothing to do on the PC after an update: the voice lab runs once by itself the first time a voice model is ready and nobody is on the line. A voice model download that failed is tried again every 6 hours, and the turn detector is picked up when it arrives (no restart).
- Acceptance tests 40 (Today card) and 41 (using the wait).
- Removed: the call context builder of the realtime voice (the Today card replaces it for both voices) and the fixed "Sec, checking." as the only line in the wait.

## 2026-10-16 — build: local voice (owner's decision: free, on the PC)
- Calls and the voice button talk to a small voice model on the PC's graphics card, loaded all the time. No key, no cost, nothing leaves the PC for small talk. OpenAI Realtime stays as an option (More → Settings → Voice), off by default.
- Engine: llama.cpp with CUDA (prebuilt for Windows, runs all three candidates). ExLlamaV3/TabbyAPI was not picked: no Windows build without an admin compiler setup, and no Gemma 4 E4B support.
- The voice lab (More → System → Voice lab) measures Gemma 4 E4B, Qwen3.5-9B and LFM2.5-2.6B (Q4) on the PC, thinking off: load time, graphics memory, first word, tokens per second, tool-call reliability, facts kept, English and German naturalness (Claude judges blind). It picks the fastest that reliably calls the tool and sounds natural, and shows the numbers. It also times Whisper (auto and English-only), Kokoro and Piper, and the turn detector. Run diagnosis lists all of it.
- Kyutai's streaming speech to text and text to speech can be installed for the lab with one tap (about 10 GB, English and French only) and compared with Whisper + Kokoro. It goes into calls only if it wins. Moshi is not used as the voice: it can't take Claude's answers mid-call.
- The voice model only talks. Its one tool is ask_kairo: Claude does the real work. It says a short filler ("Sec, checking."), "still on it" after 6 s, then says Claude's answer in its own words. Anything for another person is still an approval card.
- Speed: one short fixed prompt read once when the call opens and kept in the model's cache; the conversation is only added to; everything on the graphics card; the answer streams to the voice sentence by sentence.
- Turn-taking: Kairo transcribes at the first short pause (0.2 s) while a turn detector (Smart Turn v3, on the processor) checks if you're done. Done: it answers at once. Not sure: it waits a bit longer. The phone still ends a turn after 1.5 s of quiet. Talking over it stops it at once.
- Bring-up items: Kairo picks what to ask or remind you about from your open loops, questions it has for you and tests in the next 48 h; Claude picks and words them every 30 minutes before calls. The voice model brings up at most two per call (urgent ones always), at a good moment, and your answers go back to Claude.
- Graphics memory: the voice model, Whisper and the voice fit in 12 GB; when a voice model is installed, the memory search runs on the processor. Voice always wins.
- The old voice answers when the voice model isn't there (not installed, starting, crashed, paused for the lab).
- Voice speed targets for every voice path: p50 < 0.8 s, p95 < 1.5 s, with two new steps: "Voice model: first word (PC)" and "Claude working (ask_kairo)".
- The update downloads llama.cpp, the three models and the turn detector once (about 14 GB, checked against their published checksums). It skips a model when the disk is short.
- Acceptance test 39 is now "Voice like a person".
- Removed: OpenAI Realtime is no longer the default voice (kept as an option). Nothing deleted.

## 2026-10-15 — build: realtime voice (owner's decision: an API for voice only)
- Calls and the voice button talk to OpenAI's realtime model (gpt-realtime-2.1-mini). Your voice goes from the iPhone straight to OpenAI and its voice comes straight back, so a reply can start about half a second after you stop, and you can talk over it like on a phone call.
- The real work still goes to Claude: the voice model has one tool, ask_kairo. It says something short ("Sec, checking") while Claude works, then says Claude's answer. It can't do anything itself; anything for another person is still an approval card.
- At call start it gets a short context: the time, two days of calendar, your top 5 open loops, the start of your profile and the memory gist. Event titles and loops are marked as data, not instructions. Claude acts only on what you said yourself on the call.
- Both sides' words go into the one conversation. An answer you hung up before hearing is saved in the chat.
- Hard monthly cap, default 10 CHF (More → Settings → Realtime voice). At 80% one push; at 100% the call goes on with the local voice and no realtime call starts until next month. Every answer's real cost is counted; if Kairo can't follow a call (and count its cost), it ends it.
- The local voice is the fallback, by itself: realtime off, no key, cap reached, OpenAI down, no internet or a dropped connection, also mid-call. The call screen shows which voice is on.
- The OpenAI key is typed in Settings, kept encrypted on the PC only, never shown again (only its last 4 characters) and never on the phone.
- More → Usage shows the month's realtime spend against the cap. More → System → Voice speed has "Call, realtime" and "Voice button, realtime" with the new targets (p50 < 0.8 s, p95 < 1.5 s). Run diagnosis lists the spend, the fallbacks and the last calls.
- Acceptance test 39 (realtime voice on the iPhone).
- Fixed: More → Usage crashed (a component wasn't imported).
- Removed: nothing yet. The local voice stays as the fallback.

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
