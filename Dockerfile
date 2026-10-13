# Kairo API image: FastAPI backend + built PWA + the official Claude Code CLI.
# The CLI is installed unmodified from npm (pinned) and logs in through
# Anthropic's own flow; its login lives in the data volume (CLAUDE_CONFIG_DIR).

FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm
# >= 2.1.295 is required for the hook's onFailure: "block" (KICKOFF 7).
ARG CLAUDE_CODE_VERSION=2.1.296
ARG KAIRO_UID=1000
ARG KAIRO_GID=1000

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && npm install -g --no-audit --no-fund "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force \
 && claude --version

WORKDIR /app/server
COPY server/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ ./
COPY --from=web /web/dist /app/web/dist
COPY scripts/entrypoint.sh /usr/local/bin/kairo-entrypoint

RUN groupadd -g "${KAIRO_GID}" kairo && useradd -m -u "${KAIRO_UID}" -g kairo kairo
USER kairo

ENV KAIRO_DATA_DIR=/srv/kairo \
    KAIRO_CONFIG_DIR=/app/config \
    KAIRO_WEB_DIST=/app/web/dist \
    CLAUDE_CONFIG_DIR=/srv/kairo/claude-config \
    PYTHONUNBUFFERED=1

EXPOSE 8000
ENTRYPOINT ["kairo-entrypoint"]
CMD ["serve"]
