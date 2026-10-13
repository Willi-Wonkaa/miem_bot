#!/usr/bin/env bash
set -euo pipefail
if [[ $(id -u) != 0 ]]; then
  echo "Run as root: bash scripts/install_monitor.sh /opt/miem-bot/pending" >&2
  exit 1
fi
release_dir=${1:?Provide release directory}
release_dir=$(realpath "$release_dir")
if [[ "$release_dir" != /opt/miem-bot/releases/* && "$release_dir" != /opt/miem-bot/staging ]]; then
  echo "Release must be inside /opt/miem-bot" >&2
  exit 1
fi
install -d -m 755 /opt/miem-bot/tools /opt/miem-bot/shared/logs
install -m 644 "$release_dir/app/host_monitor.py" /opt/miem-bot/tools/host-monitor.py
docker volume create miem-bot_monitor-data >/dev/null
monitor_dir=$(docker volume inspect --format '{{.Mountpoint}}' miem-bot_monitor-data)
chown 10001:10001 "$monitor_dir" /opt/miem-bot/shared/logs
cat > /etc/systemd/system/miem-monitor.service <<UNIT
[Unit]
Description=MIEM Docker status and indexer resource monitor
After=docker.service
Requires=docker.service

[Service]
ExecStart=/usr/bin/python3 /opt/miem-bot/tools/host-monitor.py --directory $monitor_dir --log-dir /opt/miem-bot/shared/logs
Restart=always
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now miem-monitor.service
systemctl restart miem-monitor.service
