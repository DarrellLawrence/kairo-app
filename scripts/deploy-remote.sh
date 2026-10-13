#!/usr/bin/env bash
# Runs ON kairo-home, piped in by scripts/deploy.sh. Do not run by hand
# unless you know why.
#
#   deploy <sha>   check out <sha>, back up the DB, rebuild, health-check,
#                  tag deploy-<timestamp>; on a failed health check, roll back
#   rollback       downgrade the DB to the previous deploy's schema, check out
#                  the previous deploy tag, rebuild, health-check
#
# Migration files must be named <revision>_<slug>.py with sortable revision
# ids (0001, 0002, ...) and a linear history: rollback relies on it.
set -euo pipefail

DIR=${KAIRO_DEPLOY_DIR:-/opt/kairo}
COMPOSE=${KAIRO_DEPLOY_COMPOSE:-docker compose}
HEALTH_CMD=${KAIRO_DEPLOY_HEALTH_CMD:-curl -fsS --max-time 5 http://127.0.0.1:8000/health}
HEALTH_WAIT_S=${KAIRO_DEPLOY_HEALTH_WAIT_S:-120}
KEEP_TAGS=${KAIRO_DEPLOY_KEEP_TAGS:-10}

cd "$DIR"
if [[ -f .env ]]; then set -a; . ./.env; set +a; fi
DATA=${KAIRO_HOST_DATA_DIR:-/srv/kairo}
DB="$DATA/db/kairo.sqlite"

log() { echo "[kairo-home] $*"; }

healthy() {
  local waited=0
  while (( waited < HEALTH_WAIT_S )); do
    if $HEALTH_CMD >/dev/null 2>&1; then return 0; fi
    sleep 3; waited=$((waited + 3))
  done
  return 1
}

backup_db() {
  [[ -f "$DB" ]] || { log "no database yet, skipping backup"; return 0; }
  mkdir -p "$DATA/db/backups"
  local dest
  dest="$DATA/db/backups/pre-deploy-$(date +%Y%m%d-%H%M%S).sqlite"
  python3 - "$DB" "$dest" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
PY
  log "database backed up to $dest"
}

head_revision_at() {
  # Highest migration revision in the tree of git ref $1 (linear history).
  git ls-tree --name-only "$1" server/migrations/versions/ \
    | sed -n 's#.*/\([0-9][0-9]*\)_.*\.py$#\1#p' | sort | tail -n 1
}

latest_deploy_tag() { git tag --list 'deploy-*' --sort=-refname | sed -n "${1}p"; }

rebuild() { $COMPOSE up -d --build --remove-orphans; }

do_deploy() {
  local sha=$1 prev
  git fetch -q --tags origin
  git cat-file -e "$sha^{commit}" || { log "unknown commit $sha"; exit 1; }
  prev=$(git rev-parse HEAD)
  backup_db
  git checkout -q --detach "$sha"
  log "building and starting $sha"
  if rebuild && healthy; then
    local tag
    tag="deploy-$(date +%Y%m%d-%H%M%S)"
    git tag "$tag" "$sha"
    git tag --list 'deploy-*' --sort=-refname | tail -n +"$((KEEP_TAGS + 1))" | xargs -r git tag -d >/dev/null
    log "healthy. tagged $tag"
    return 0
  fi
  log "health check FAILED for $sha, rolling back to $prev"
  roll_to "$prev"
  exit 1
}

roll_to() {
  local target=$1 rev
  rev=$(head_revision_at "$target")
  if [[ -n "$rev" ]]; then
    log "downgrading database schema to $rev (with the current code, which knows every migration)"
    $COMPOSE run --rm --no-deps api alembic downgrade "$rev" || log "WARNING: downgrade failed; check before using the app"
  fi
  git checkout -q --detach "$target"
  rebuild
  if healthy; then log "now running $(git rev-parse --short HEAD)"; else log "WARNING: still unhealthy after rollback"; exit 1; fi
}

do_rollback() {
  local current previous
  current=$(latest_deploy_tag 1)
  previous=$(latest_deploy_tag 2)
  [[ -n "$previous" ]] || { log "no previous deploy tag to roll back to"; exit 1; }
  backup_db
  log "rolling back from $current to $previous"
  roll_to "$previous"
  git tag -d "$current" >/dev/null
}

case "${1:-}" in
  deploy) do_deploy "${2:?missing sha}" ;;
  rollback) do_rollback ;;
  *) echo "usage: deploy-remote.sh deploy <sha> | rollback" >&2; exit 2 ;;
esac
