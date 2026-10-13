#!/usr/bin/env bash
# Deploy Kairo to kairo-home (KICKOFF 2b). Run on the laptop, from ~/kairo.
#
#   scripts/deploy.sh             deploy origin/main (tests must pass first)
#   scripts/deploy.sh --rollback  go back to the previous deploy
#
# The remote half is scripts/deploy-remote.sh, piped over ssh, so nothing is
# ever edited by hand on kairo-home.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO=$(pwd)

if [[ -f .env ]]; then
  set -a; . ./.env; set +a
fi
HOST=${KAIRO_DEPLOY_HOST:-kairo-home}
DIR=${KAIRO_DEPLOY_DIR:-/opt/kairo}

mode=deploy
case "${1:-}" in
  "") ;;
  --rollback) mode=rollback ;;
  -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
  *) echo "unknown option: $1" >&2; exit 2 ;;
esac

remote() {
  if [[ "$HOST" == "local" ]]; then
    bash -s -- "$@" < "$REPO/scripts/deploy-remote.sh"
  else
    # shellcheck disable=SC2029  # args are expanded locally on purpose
    ssh "$HOST" "KAIRO_DEPLOY_DIR='$DIR' bash -s -- $*" < "$REPO/scripts/deploy-remote.sh"
  fi
}

if [[ "$mode" == "rollback" ]]; then
  echo "==> Rolling back $HOST to the previous deploy"
  KAIRO_DEPLOY_DIR=$DIR remote rollback
  exit 0
fi

echo "==> Checking the working tree"
if [[ -n "$(git status --porcelain)" ]]; then
  echo "Uncommitted changes. Commit and push first." >&2
  exit 1
fi
git fetch -q origin main
sha=$(git rev-parse origin/main)
if [[ "$(git rev-parse HEAD)" != "$sha" ]]; then
  echo "HEAD is not origin/main ($sha). Deploy only what is pushed to main." >&2
  exit 1
fi

echo "==> Running tests"
py="$REPO/server/.venv/bin/python"
[[ -x "$py" ]] || py=python3
(cd server && "$py" -m pytest -q) || { echo "Tests failed. Not deploying." >&2; exit 1; }
(cd web && npm ci --silent --no-audit --no-fund && npm run -s build >/dev/null) || { echo "Web build failed. Not deploying." >&2; exit 1; }

echo "==> Deploying $sha to $HOST:$DIR"
KAIRO_DEPLOY_DIR=$DIR remote deploy "$sha"
