#!/bin/sh
set -e

MAX_ATTEMPTS=10
ATTEMPT=1

echo "entrypoint: running database migrations (alembic upgrade head)..."

until alembic upgrade head; do
  if [ "$ATTEMPT" -ge "$MAX_ATTEMPTS" ]; then
    echo "entrypoint: migrations failed after ${MAX_ATTEMPTS} attempts — giving up." >&2
    exit 1
  fi
  echo "entrypoint: migration attempt ${ATTEMPT} failed, retrying in 3s..." >&2
  ATTEMPT=$((ATTEMPT + 1))
  sleep 3
done

echo "entrypoint: migrations complete. Starting API server..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1