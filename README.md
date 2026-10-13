# Kairo

Single-user personal assistant. Spec: [docs/KICKOFF.md](docs/KICKOFF.md) (source of truth). Rules for Claude Code: [CLAUDE.md](CLAUDE.md).

- `server/` FastAPI backend, SQLModel + SQLite, Alembic, brain adapters (`server/app/brain/`)
- `web/` React + Vite PWA (six tabs; Chat works, the rest arrive with later milestones)
- `config/` who the owner is (`profile.yaml`), permissions, schedule, personality
- `installer/` Windows installer, updater, rollback, uninstaller (kairo-home)
- `scripts/build-release.sh` builds the setup zip

## Dev (laptop, inside WSL)

```bash
cd ~/kairo/server
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest                     # all tests, uses the fake CLI
.venv/bin/alembic upgrade head                 # creates devdata/db/kairo.sqlite
KAIRO_CLAUDE_BIN=$PWD/tests/fakes/claude .venv/bin/uvicorn app.main:create_app --factory --reload
```
Leave out `KAIRO_CLAUDE_BIN=...` to chat with the real `claude` CLI (must be logged in).

```bash
cd ~/kairo/web && npm ci && npm run dev        # http://localhost:5173, proxies to :8000
```

## kairo-home (Windows): install, update, rollback, uninstall

Everything on kairo-home is double-click. Nothing gets wiped, no Docker, no WSL.

**First install**
1. BIOS, once: set "Restore on AC power loss" (sometimes "AC Back" or "Power On after power failure") to **On**.
2. Get the setup zip: on GitHub open the kairo repo → **Releases** (right side) → newest release → download `Kairo-Setup-….zip`. Or build one on the laptop: `scripts/build-release.sh` → `dist/`.
3. On kairo-home: right-click the zip → **Extract All** → open the folder → double-click **Install Kairo.bat** → Windows asks for admin rights → **Yes**.
4. Follow the screen. It installs Python, Tailscale and Claude Code, sets up the boot task, turns off sleep, sets Windows Update to Sundays 04:00, offers BitLocker if the PC has a TPM, then walks you through three logins (Tailscale, Claude, GitHub for updates).
5. Restart the PC once and **don't log in**. After about 5 minutes, on the iPhone: Kairo → **More** → "Last boot check" should be all green (Claude, Tailscale, HTTPS). Then send a chat message.

**Afterwards, in `C:\Kairo`:**
- **Update Kairo.bat**: downloads the newest *tested* release from GitHub (CI publishes one only after all tests and the Windows install test pass), checks its checksum, installs it, and switches back by itself if the new version doesn't come up.
- **Rollback Kairo.bat**: back to the previous version (database schema first).
- **Kairo Logins.bat**: redo a login (e.g. Claude login expired).
- **Kairo Check.bat**: the same checks the PC runs after every reboot (Kairo up, own account, Claude, Tailscale, HTTPS).

Kairo runs as its own Windows account **kairo-svc**: no admin rights, hidden from the sign-in screen, can only use `C:\Kairo`.
- **Uninstall Kairo.bat**: removes Kairo; your data stays unless you type DELETE.

Logs: `C:\Kairo\data\logs\` (`kairo.log` for the app, `install-*.log` for every install/update).

## Tests

```bash
cd server && .venv/bin/python -m pytest                                   # fast, fakes only
KAIRO_REAL_CLI=1 .venv/bin/python -m pytest tests/test_realcli.py -o addopts="" -v   # real Claude CLI (a few tiny calls)
```
Run the real-CLI tests before a release that touches the hook, the workspace config, the brain adapter or prompts.

## Status

Milestones A, A2 and B (the gate) are built. Kairo can read Gmail, Calendar and the vault, keep tasks and notes, label and draft by itself, and asks on the Approvals screen before it sends, replies, answers an invite or deletes anything.
