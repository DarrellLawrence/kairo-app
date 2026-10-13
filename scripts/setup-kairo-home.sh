#!/usr/bin/env bash
# One-time setup of kairo-home (KICKOFF 6, "By Claude Code over SSH").
# Run on kairo-home as the normal user (it uses sudo):
#   KAIRO_REPO_URL=git@github.com:<owner>/kairo.git bash setup-kairo-home.sh
# Safe to run again. Phase 3 items (NVIDIA driver, quiet mode, heartbeat,
# backups) are NOT here yet.
#
# Before running: Ubuntu Server 24.04 installed, `sudo tailscale up --ssh` done,
# and a GitHub deploy key added (see README, "kairo-home first-time setup").
set -euo pipefail

REPO_URL=${KAIRO_REPO_URL:?set KAIRO_REPO_URL to the SSH clone URL of your kairo repo}
USER_NAME=$(id -un)

echo "==> Unattended security updates ON, automatic reboots OFF"
sudo apt-get update -q
sudo apt-get install -y -q unattended-upgrades curl git python3 ufw ca-certificates
sudo tee /etc/apt/apt.conf.d/52kairo-unattended >/dev/null <<'CONF'
Unattended-Upgrade::Automatic-Reboot "false";
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF

echo "==> Docker Engine + Compose plugin (Docker's apt repo)"
if ! command -v docker >/dev/null; then
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -q
  sudo apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
sudo usermod -aG docker "$USER_NAME"
sudo systemctl enable --now docker

echo "==> Firewall: deny incoming except over Tailscale"
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow in on tailscale0
sudo ufw --force enable
# Note: Docker publishes ports past ufw. That is why compose binds 127.0.0.1 only.

echo "==> Data and code directories"
sudo mkdir -p /srv/kairo/{db,vault,inbox,logs,workspace,claude-config} /opt/kairo
sudo chown -R "$USER_NAME:$USER_NAME" /srv/kairo /opt/kairo

if [[ ! -d /opt/kairo/.git ]]; then
  git clone "$REPO_URL" /opt/kairo
fi
if [[ ! -f /opt/kairo/.env ]]; then
  cp /opt/kairo/.env.example /opt/kairo/.env
  sed -i "s#^KAIRO_HOST_DATA_DIR=.*#KAIRO_HOST_DATA_DIR=/srv/kairo#; s#^KAIRO_UID=.*#KAIRO_UID=$(id -u)#; s#^KAIRO_GID=.*#KAIRO_GID=$(id -g)#" /opt/kairo/.env
fi

echo "==> Serve the app on the tailnet only (HTTPS via tailscale serve)"
sudo tailscale serve --bg 8000

cat <<'NEXT'

Done. Next steps (README, "kairo-home first-time setup"):
  1. Log out and back in (docker group).
  2. cd /opt/kairo && docker compose up -d --build
  3. Log the Claude CLI in, once:  docker compose run --rm -it api claude   (then /login)
NEXT
