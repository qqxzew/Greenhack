#!/usr/bin/env bash
# Argus — stop everything started by ./start.sh
cd "$(dirname "$0")"
LOG_DIR="./.run"

stopped=0
for p in traffic backend frontend; do
  pidfile="$LOG_DIR/$p.pid"
  if [ -f "$pidfile" ]; then
    pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null
      sleep 0.3
      kill -9 "$pid" 2>/dev/null
      echo "stopped $p (pid $pid)"
      stopped=$((stopped+1))
    fi
    rm -f "$pidfile"
  fi
done

# Belt and suspenders: catch anything left over by name.
pkill -f "live_traffic.py"    2>/dev/null && echo "killed leftover live_traffic.py"   || true
pkill -f "uvicorn main:app"   2>/dev/null && echo "killed leftover uvicorn"           || true
pkill -f "http.server 8765"   2>/dev/null && echo "killed leftover http.server"       || true

if [ $stopped -eq 0 ]; then
  echo "nothing was running"
fi
