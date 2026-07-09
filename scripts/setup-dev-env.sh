#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

API_ENV="apps/api/.env"
WORKER_ENV="apps/worker/.env"
WS_ENV="apps/ws-server/.env"

gen_secret() {
  python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null \
    || openssl rand -hex 32
}

provision() {
  local target="$1"
  local example="$2"
  if [ -f "$target" ]; then
    echo "existing"
    return 0
  fi
  cp "$example" "$target"
  echo "created"
}

echo "Provisioning local development .env files..."

api_status="$(provision "$API_ENV" "apps/api/.env.example")"
echo "  [$api_status] $API_ENV"

worker_status="$(provision "$WORKER_ENV" "apps/worker/.env.example")"
echo "  [$worker_status] $WORKER_ENV"

ws_status="$(provision "$WS_ENV" "apps/ws-server/.env.example")"
echo "  [$ws_status] $WS_ENV"

if [ "$api_status" = "created" ] && [ "$ws_status" = "created" ]; then
  SECRET="$(gen_secret)"
  sed -i.bak "s/^SECRET_KEY=.*/SECRET_KEY=${SECRET}/" "$API_ENV" && rm -f "$API_ENV.bak"
  sed -i.bak "s/^JWT_SECRET=.*/JWT_SECRET=${SECRET}/" "$WS_ENV" && rm -f "$WS_ENV.bak"
  echo "  [secret] generated a shared dev-only SECRET_KEY / JWT_SECRET"
fi

echo "Done. Run: docker compose up -d"