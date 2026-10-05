#!/bin/sh
# One-shot verification gate used by the "verify" compose service.
# Runs the test suite, then exercises the running service over real HTTP,
# including the DST spring-forward/fall-back boundaries. Exits non-zero if
# anything fails.
set -eu

echo "==> Unit / API tests"
python -m pytest -q

if [ -z "${BASE_URL:-}" ]; then
    echo "==> BASE_URL not set; starting uvicorn locally for the smoke run"
    python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 &
    APP_PID=$!
    BASE_URL="http://127.0.0.1:8000"
    export BASE_URL
    trap 'kill "$APP_PID" 2>/dev/null || true' EXIT
fi

echo "==> HTTP smoke against $BASE_URL"
python scripts/smoke.py
