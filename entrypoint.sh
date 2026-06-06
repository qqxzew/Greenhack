#!/usr/bin/env bash
set -e

PORT="${PORT:-8080}"
cd /app/Argus

# Start uvicorn in the background, capture PID
uvicorn main:app --host 0.0.0.0 --port "$PORT" &
UVICORN_PID=$!

# Wait up to 30 s for the backend to be healthy
echo "Waiting for backend on :$PORT ..."
for i in $(seq 1 60); do
  if curl -sf "http://localhost:$PORT/v1/health" >/dev/null 2>&1; then
    echo "Backend up."
    break
  fi
  sleep 0.5
done

# With ANTHROPIC_API_KEY -> real Claude calls (--live).
# Without it             -> synthetic traffic (no API calls, no spend).
if [ -n "$ANTHROPIC_API_KEY" ]; then
  echo "Starting live traffic (real Claude calls) ..."
  python -u live_traffic.py --live \
    --max-calls  "${LIVE_MAX_CALLS:-40}" \
    --interval   "${LIVE_INTERVAL:-3}" \
    --max-tokens "${LIVE_MAX_TOKENS:-150}" &
else
  echo "No ANTHROPIC_API_KEY -- starting synthetic traffic ..."
  python -u live_traffic.py \
    --max-calls  "${LIVE_MAX_CALLS:-40}" \
    --interval   "${LIVE_INTERVAL:-3}" \
    --max-tokens "${LIVE_MAX_TOKENS:-150}" &
fi

# Keep container alive; exit only if uvicorn dies
wait $UVICORN_PID