#!/bin/sh
# Container entrypoint: create data dirs, migrate, then serve.
set -eu

: "${KAIRO_DATA_DIR:=/srv/kairo}"
mkdir -p "$KAIRO_DATA_DIR/db" "$KAIRO_DATA_DIR/vault" "$KAIRO_DATA_DIR/inbox" \
         "$KAIRO_DATA_DIR/logs" "$KAIRO_DATA_DIR/workspace" "${CLAUDE_CONFIG_DIR:-$KAIRO_DATA_DIR/claude-config}"

case "${1:-serve}" in
  serve)
    cd /app/server
    alembic upgrade head
    exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers
    ;;
  *)
    exec "$@"
    ;;
esac
