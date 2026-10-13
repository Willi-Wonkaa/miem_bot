#!/usr/bin/env bash
# Started by the deployment workflow, under its existing deployment account.
set -euo pipefail
release_dir=${1:?Missing release directory}
release_dir=$(realpath "$release_dir")
if [[ "$release_dir" != /opt/miem-bot/releases/* ]]; then
  echo "Release must be inside /opt/miem-bot/releases" >&2
  exit 1
fi
directory=/opt/miem-bot/shared/host
mkdir -p "$directory"
if [[ -f "$directory/monitor.pid" ]]; then
  previous=$(cat "$directory/monitor.pid")
  if [[ "$previous" =~ ^[0-9]+$ ]]; then
    args=$(ps -p "$previous" -o args= || true)
    if [[ "$args" == *"$directory/host-monitor.py"* ]]; then
      kill "$previous" || true
    fi
  fi
fi
cp "$release_dir/app/host_monitor.py" "$directory/host-monitor.py"
nohup python3 "$directory/host-monitor.py" --directory "$directory" --log-dir "$directory" \
  >"$directory/monitor-console.txt" 2>&1 </dev/null &
echo "$!" > "$directory/monitor.pid"
for attempt in $(seq 1 20); do
  if python3 - "$directory/host.json" "$(cat "$directory/monitor.pid")" <<'PY'
import json, sys, time, os
try:
    os.kill(int(sys.argv[2]),0)
    value=json.load(open(sys.argv[1]))
    assert time.time()-value['updated']<10
except (OSError,ValueError,KeyError,AssertionError):
    raise SystemExit(1)
PY
  then
    echo "Resource monitor ready"
    exit 0
  fi
  sleep 1
done
echo "Resource monitor failed to become ready" >&2
exit 1
