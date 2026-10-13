#!/usr/bin/env bash
# Read-only diagnostics; safe to run while deployment/indexing is in progress.
set -euo pipefail
if [[ "${1:-}" == repair-monitor ]]; then
  # Run outside deploy.sh: the replacement process cannot inherit its lock descriptor.
  bash /opt/miem-bot/current/scripts/start_monitor.sh /opt/miem-bot/current
fi
docker ps -a --filter label=com.docker.compose.project=miem-bot \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
free -m
docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'
python3 - <<'PY'
import json
from pathlib import Path
root = Path('/opt/miem-bot/shared')
allowed = {'GENERATOR_MODEL', 'EMBED_MODEL', 'EMBED_BATCH_SIZE', 'EMBED_THREADS',
           'INDEX_PARSE_WORKERS', 'INDEX_CPU_LIMIT', 'INDEX_MEM_LIMIT',
           'RAG_CPU_LIMIT', 'RAG_MEM_LIMIT'}
values = {}
for line in (root / '.env').read_text().splitlines():
    if '=' in line and not line.lstrip().startswith('#'):
        name, value = line.split('=', 1)
        if name.strip() in allowed:
            values[name.strip()] = value.strip()
print('Configured resources/model:', json.dumps(values, ensure_ascii=False))
host = root / 'host' / 'host.json'
if host.is_file():
    print('Host monitor:', host.read_text())
PY
rag_id=$(docker ps -q --filter label=com.docker.compose.project=miem-bot \
  --filter label=com.docker.compose.service=rag | head -n 1)
if [[ -z "$rag_id" ]]; then
  echo 'RAG container is not running' >&2
  exit 1
fi
docker exec "$rag_id" python -m app.rag.smoke --timeout 5
docker exec -i "$rag_id" python - <<'PY'
import json
import sqlite3
with sqlite3.connect('file:/data/monitor/control.sqlite3?mode=ro', uri=True) as db:
    db.row_factory = sqlite3.Row
    print('Components:', json.dumps([dict(row) for row in db.execute(
        'SELECT name, state, started, updated FROM components ORDER BY name')]))
    for row in db.execute("SELECT key,value FROM kv WHERE key IN ('index_progress','index_publication')"):
        print(row['key'] + ': ' + row['value'])
PY
if docker inspect miem-bot-indexer >/dev/null 2>&1; then
  docker inspect --format '{{json .State}}' miem-bot-indexer
  docker logs --tail 60 miem-bot-indexer
fi
