# Kairo

Single-user personal assistant. Spec: [docs/KICKOFF.md](docs/KICKOFF.md) (source of truth). Rules for Claude Code: [CLAUDE.md](CLAUDE.md).

- `server/` FastAPI backend, SQLModel + SQLite, Alembic, brain adapters (`server/app/brain/`)
- `web/` React + Vite PWA (six tabs; Chat works, the rest arrive with later milestones)
- `config/` who the owner is (`profile.yaml`), permissions, schedule, personality
- `scripts/` deploy, rollback, kairo-home setup

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

Whole stack in Docker: `cp .env.example .env && docker compose up -d --build` → http://127.0.0.1:8000.

## kairo-home first-time setup

You do steps 1–3 by hand once; after that everything goes through `scripts/deploy.sh`.

1. **Deploy key, so kairo-home can read the private repo.** On kairo-home:
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/kairo_deploy -N "" -C kairo-home
   printf 'Host github.com\n  IdentityFile ~/.ssh/kairo_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
   cat ~/.ssh/kairo_deploy.pub
   ```
   On GitHub: open the kairo repo → **Settings** → **Deploy keys** → **Add deploy key** → Title `kairo-home` → paste the key → leave **Allow write access** unticked → **Add key**.
2. **Run the setup script.** From the laptop: `scp scripts/setup-kairo-home.sh kairo-home:` then on kairo-home:
   `KAIRO_REPO_URL=git@github.com:<owner>/kairo.git bash setup-kairo-home.sh`, then log out and back in.
3. **Start Kairo and log the CLI in (once):**
   ```bash
   cd /opt/kairo && docker compose up -d --build
   docker compose run --rm -it api claude        # type /login, open the link, paste the code, then /exit
   ```
   The login is stored in `/srv/kairo/claude-config`, so it survives rebuilds.
4. **iPhone:** Safari → `https://kairo-home.<your-tailnet>.ts.net` → **Share** → **Add to Home Screen** → **Add**. (The iPhone needs the Tailscale app, signed in and connected.)

## Deploy and rollback (from the laptop)

```bash
scripts/deploy.sh             # only origin/main, only after tests + web build pass
scripts/deploy.sh --rollback  # previous deploy, database schema downgraded first
```
Each deploy backs up the database to `/srv/kairo/db/backups/`, health-checks, tags `deploy-<timestamp>`, and rolls back by itself if the health check fails. Migrations must be named `<NNNN>_<slug>.py` (linear history), because rollback relies on it.

## Status

Milestone A (skeleton) code is done. The gate (Milestone B) is **not** built yet: until then the brain runs with **no tools at all** (`--tools ""`, no MCP servers), so it can only talk.
