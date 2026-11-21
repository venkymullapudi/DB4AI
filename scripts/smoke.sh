#!/usr/bin/env bash
# End-to-end smoke test of the Docker deployment (docker-compose.yml):
# build, migrate, create a user, write an item over HTTP, run the maintainers, search for it.
# Usage: scripts/smoke.sh   (needs docker compose and curl; tears everything down, volumes included)
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=${SMOKE_PROJECT:-db4ai-smoke}
API=${SMOKE_API:-http://localhost:8000}
dc() { docker compose -p "$PROJECT" "$@"; }
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then dc logs --no-color --tail 200 || true; fi
  dc down -v --remove-orphans >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

echo "== build and start"
dc up -d --build api worker

echo "== wait for the API"
for _ in $(seq 1 90); do
  curl -fsS "$API/healthz" >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS "$API/healthz" | grep -q '"ok":true'

echo "== pgvector is in use"
dc exec -T postgres psql -U db4ai -d db4ai -tAc \
  "select format_type(atttypid, atttypmod) from pg_attribute
    where attrelid = 'item_chunks'::regclass and attname = 'embedding'" | grep -q '^vector'

echo "== create a user"
TOKEN=$(dc exec -T api db4ai user smoke --team eng:owner --admin 2>/dev/null | json 'd["token"]')
AUTH="Authorization: Bearer $TOKEN"

echo "== write an item"
ITEM=$(curl -fsS -X POST "$API/items" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"type":"memory","title":"Smoke launch date","body":"The zebra launch moved to April 15.","team":"eng"}' \
  | json 'd["item_id"]')
curl -fsS "$API/items/$ITEM" -H "$AUTH" | grep -q 'zebra'

echo "== run the maintainers once"
dc exec -T api db4ai maintain --once >/dev/null

echo "== search finds it, with a chunk (the worker may still be finishing the job)"
for _ in $(seq 1 30); do
  RESULT=$(curl -fsS -G "$API/search" --data-urlencode "q=zebra launch" -H "$AUTH")
  echo "$RESULT" | json "'ok' if any(r['id']=='$ITEM' and r.get('chunk') for r in d) else sys.exit(1)" 2>/dev/null && break
  sleep 1
done
echo "$RESULT" | json "'ok' if any(r['id']=='$ITEM' and r.get('chunk') for r in d) else sys.exit('item not found with a chunk: '+json.dumps(d))"

echo "smoke test passed"
