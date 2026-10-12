#!/usr/bin/env bash
set -euo pipefail
if [[ $(id -u) != 0 ]]; then
  echo "Run bootstrap as root" >&2
  exit 1
fi
if [[ $# != 1 || ! -f $1 ]]; then
  echo "Usage: bash bootstrap.sh /path/to/deploy-key.pub" >&2
  exit 1
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends docker.io docker-compose-v2 docker-buildx ca-certificates
systemctl enable --now docker
if ! id miem-deploy >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash miem-deploy
fi
usermod -aG docker miem-deploy
install -d -m 700 -o miem-deploy -g miem-deploy /home/miem-deploy/.ssh
touch /home/miem-deploy/.ssh/authorized_keys
public_key=$(cat "$1")
if ! grep -qxF "$public_key" /home/miem-deploy/.ssh/authorized_keys; then
  printf '%s\n' "$public_key" >> /home/miem-deploy/.ssh/authorized_keys
fi
chown miem-deploy:miem-deploy /home/miem-deploy/.ssh/authorized_keys
chmod 600 /home/miem-deploy/.ssh/authorized_keys
install -d -m 750 -o miem-deploy -g miem-deploy \
  /opt/miem-bot /opt/miem-bot/releases /opt/miem-bot/incoming \
  /opt/miem-bot/shared /opt/miem-bot/shared/raw
echo "Bootstrap complete"
