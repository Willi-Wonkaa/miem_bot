#!/usr/bin/env bash
# Run on the server as miem-deploy. The source archive is uploaded by remote_deploy.py.
set -euo pipefail
release=${1:?Missing commit SHA}
mode=${2:-deploy}
if [[ ! $release =~ ^[a-f0-9]{40}$ ]]; then
  echo "Invalid commit SHA" >&2
  exit 1
fi
if [[ "$mode" != prepare && "$mode" != deploy ]]; then
  echo "Mode must be prepare or deploy" >&2
  exit 1
fi
root=/opt/miem-bot
exec 9>"$root/deploy.lock"
flock -n 9 || { echo "Another deployment is running" >&2; exit 1; }
target="$root/releases/$release"
archive="$root/incoming/$release.tar.gz"
mkdir -p "$target"
tar -xzf "$archive" -C "$target"

# Committed data takes precedence; data uploaded directly remains usable too.
for filename in MIEM_knowledge_base_unified.zip HSE_Answers_RU.xlsx; do
  if [[ -f "$target/data/raw/$filename" ]]; then
    cp "$target/data/raw/$filename" "$root/shared/raw/$filename"
  fi
done
if [[ ! -f "$root/shared/.env" || ! -f "$root/shared/raw/MIEM_knowledge_base_unified.zip" ]]; then
  echo "Deployment needs shared/.env and shared/raw/MIEM_knowledge_base_unified.zip" >&2
  exit 1
fi
python3 - "$root/shared/.env" <<'PY'
import sys
values = dict(line.split('=', 1) for line in open(sys.argv[1])
              if '=' in line and not line.lstrip().startswith('#'))
for name in ('TELEGRAM_BOT_TOKEN', 'POLZA_API_KEY', 'RAG_API_TOKEN'):
    value = values.get(name, '').strip().strip('\"\'')
    if not value or value.startswith('<'):
        raise SystemExit(f'Fill {name} in shared/.env')
PY

ln -sfn "$root/shared/.env" "$target/.env"
mkdir -p "$root/shared/logs"
ln -sfn "$root/shared/logs" "$target/logs"
# Keep the tracked raw directory, and bind the shared raw directory through an override.
cat > "$target/compose.server.yaml" <<'YAML'
services:
  indexer:
    volumes:
      - /opt/miem-bot/shared/raw:/data/raw:ro
YAML
export RELEASE_TAG="$release"
compose=(docker compose --project-name miem-bot --project-directory "$target"
  -f "$target/compose.yaml" -f "$target/compose.server.yaml")
"${compose[@]}" config --quiet
"${compose[@]}" build bot rag
docker run --rm --user 0 --entrypoint python \
  -v "$root/shared/logs:/data/logs" "miem-bot:$release" \
  -c 'import os; os.chown("/data/logs",10001,10001); os.chmod("/data/logs",0o755)'

if [[ "$mode" == prepare ]]; then
  ln -sfn "$target" "$root/pending"
  echo "Prepared $release; services and indexing were not started"
  exit 0
fi

# Indexing is an explicit maintenance task, never an implicit deployment step.
"${compose[@]}" --profile index run --rm --no-deps --entrypoint python \
  indexer -m app.rag.check_index

previous=$(readlink -f "$root/current" 2>/dev/null || true)
rollback() {
  echo "Deployment failed; restoring previous release" >&2
  if [[ -n "$previous" && -d "$previous" ]]; then
    export RELEASE_TAG="$(basename "$previous")"
    docker compose --project-name miem-bot --project-directory "$previous" \
      -f "$previous/compose.yaml" -f "$previous/compose.server.yaml" \
      up -d --no-build --force-recreate --wait --wait-timeout 300
  else
    "${compose[@]}" stop bot rag || true
  fi
}
trap rollback ERR
"${compose[@]}" up -d --no-build --force-recreate --wait --wait-timeout 120 service
"${compose[@]}" up -d --no-build --force-recreate --wait --wait-timeout 300 redis rag bot
ln -sfn "$target" "$root/current"
trap - ERR
echo "Deployed $release"
