#!/usr/bin/env bash
# Run on the server as miem-deploy. The source archive is uploaded by remote_deploy.py.
set -euo pipefail
release=${1:?Missing commit SHA}
mode=${2:-deploy}
if [[ ! $release =~ ^[a-f0-9]{40}$ ]]; then
  echo "Invalid commit SHA" >&2
  exit 1
fi
if [[ "$mode" != prepare && "$mode" != deploy && "$mode" != index ]]; then
  echo "Mode must be prepare, deploy or index" >&2
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
# The indexer runs as UID 10001; bootstrap's deployment-only mode 750 blocks it.
chmod 755 "$root/shared/raw"
for filename in MIEM_knowledge_base_unified.zip HSE_Answers_RU.xlsx; do
  if [[ -f "$root/shared/raw/$filename" ]]; then
    chmod 644 "$root/shared/raw/$filename"
  fi
done
if [[ ! -f "$root/shared/.env" || ! -f "$root/shared/raw/MIEM_knowledge_base_unified.zip" ]]; then
  echo "Deployment needs shared/.env and shared/raw/MIEM_knowledge_base_unified.zip" >&2
  exit 1
fi
python3 - "$root/shared/.env" "$mode" <<'PY'
import sys
values = dict(line.split('=', 1) for line in open(sys.argv[1])
              if '=' in line and not line.lstrip().startswith('#'))
required = ['TELEGRAM_BOT_TOKEN', 'POLZA_API_KEY', 'RAG_API_TOKEN']
if sys.argv[2] != 'prepare':
    required.append('SERVICE_BOT_TOKEN')
for name in required:
    value = values.get(name, '').strip().strip('\"\'')
    if not value or value.startswith('<'):
        raise SystemExit(f'Fill {name} in shared/.env')
PY

ln -sfn "$root/shared/.env" "$target/.env"
mkdir -p "$root/shared/logs"
mkdir -p "$root/shared/host"
mkdir -p "$root/shared/history"
ln -sfn "$root/shared/history" "$target/data/history"
ln -sfn "$root/shared/logs" "$target/logs"
ln -sfn "$root/shared/host" "$target/host-monitor"
# Keep the tracked raw directory, and bind the shared raw directory through an override.
cat > "$target/compose.server.yaml" <<'YAML'
services:
  service:
    volumes:
      - /opt/miem-bot/shared/history:/data/history
  bot:
    volumes:
      - /opt/miem-bot/shared/history:/data/history
  indexer:
    volumes:
      - /opt/miem-bot/shared/raw:/data/raw:ro
YAML
export RELEASE_TAG="$release"
compose=(docker compose --project-name miem-bot --project-directory "$target"
  -f "$target/compose.yaml" -f "$target/compose.server.yaml")
"${compose[@]}" config --quiet
"${compose[@]}" build bot rag
"${compose[@]}" --profile index run --rm --no-deps --entrypoint python indexer -c \
  'import json,zipfile; from pathlib import Path; p=Path("/data/raw/MIEM_knowledge_base_unified.zip"); z=zipfile.ZipFile(p); m=[n for n in z.namelist() if Path(n).name=="manifest.json"]; assert len(m)==1, "Expected one manifest"; docs=json.loads(z.read(m[0]))["documents"]; assert docs, "Empty corpus"; faq=Path("/data/raw/HSE_Answers_RU.xlsx"); assert not faq.exists() or faq.open("rb").read(1); print("Readable source documents:", len(docs))'
docker run --rm --user 0 --entrypoint python \
  -v "$root/shared/logs:/data/logs" -v "$root/shared/history:/data/history" "miem-bot:$release" \
  -c 'import os; os.chown("/data/logs",10001,10001); os.chmod("/data/logs",0o755); os.chown("/data/history",10001,10001); os.chmod("/data/history",0o700)'

if [[ "$mode" == prepare ]]; then
  ln -sfn "$target" "$root/pending"
  echo "Prepared $release; services and indexing were not started"
  exit 0
fi

# A cold or partial corpus is supported; RAG responds with progress while it warms up.

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
bash "$target/scripts/start_monitor.sh" "$target"
"${compose[@]}" up -d --no-build --force-recreate --wait --wait-timeout 300 redis rag bot
"${compose[@]}" exec -T rag python -m app.rag.smoke
"${compose[@]}" exec -T redis redis-cli ping
"${compose[@]}" ps
ln -sfn "$target" "$root/current"
trap - ERR
echo "Deployed $release"
if [[ "$mode" == index ]]; then
  if docker inspect miem-bot-indexer >/dev/null 2>&1; then
    if [[ "$(docker inspect --format '{{.State.Running}}' miem-bot-indexer)" == true ]]; then
      if [[ "$(docker inspect --format '{{.Config.Image}}' miem-bot-indexer)" != "miem-rag:$release" ]]; then
        stopped_id=$(docker inspect --format '{{.Id}}' miem-bot-indexer)
        python3 - "$root/shared/host/indexer-stop.json" "${stopped_id:0:12}" <<'PY'
import json, os, sys, tempfile, time
from pathlib import Path
path = Path(sys.argv[1])
with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
    json.dump({'container_id':sys.argv[2], 'kind':'deploy',
               'reason':'Обновление версии системы; прогресс сохранён для продолжения.',
               'updated':time.time()}, stream)
    temporary = Path(stream.name)
temporary.chmod(0o644)
os.replace(temporary, path)
PY
        docker stop --timeout 60 miem-bot-indexer >/dev/null
      fi
    fi
    if [[ "$(docker inspect --format '{{.State.Running}}' miem-bot-indexer)" != true ]]; then
      docker rm miem-bot-indexer >/dev/null
    fi
  fi
  if docker inspect miem-bot-indexer >/dev/null 2>&1; then
    index_start_id=$(docker inspect --format '{{json .Config.Env}}' miem-bot-indexer | \
      python3 -c 'import json,sys; print(next(value.split("=",1)[1] for value in json.load(sys.stdin) if value.startswith("INDEX_START_ID=")))')
  else
    index_start_id="$release-$(date +%s%N)"
    "${compose[@]}" --profile index run -d --no-deps --name miem-bot-indexer \
      -e "INDEX_START_ID=$index_start_id" indexer
    docker update --restart on-failure:5 miem-bot-indexer >/dev/null
  fi
  # Fail promptly if the writer exits before its first publication.
  ready=false
  deadline=$((SECONDS + 1800))
  while (( SECONDS < deadline )); do
    if "${compose[@]}" exec -T rag python -m app.rag.smoke --require-workers \
      --start-id "$index_start_id" --timeout 1; then
      ready=true
      break
    fi
    if [[ "$(docker inspect --format '{{.State.Running}}' miem-bot-indexer)" != true \
          && "$(docker inspect --format '{{.State.ExitCode}}' miem-bot-indexer)" != 0 ]]; then
      docker logs --tail 100 miem-bot-indexer
      echo "Indexer failed before all workers were ready" >&2
      exit 1
    fi
    sleep 10
  done
  if [[ "$ready" != true ]]; then
    echo "Indexer workers were not ready within 30 minutes" >&2
    exit 1
  fi
  echo "All indexer workers processed a document; background indexing is independent of Actions"
fi
