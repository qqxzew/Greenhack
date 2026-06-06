#!/usr/bin/env bash
# Argus — one-shot launcher.
# Brings up backend + frontend, then fires 40 REAL Claude calls of varying
# complexity (live mode — spends money; needs a key in Argus/.env), opens the
# dashboard, and tails the log. Ctrl-C stops everything cleanly.
#
# Re-runs are safe: venv + deps are only built when missing.
# Tune via env: LIVE_MAX_CALLS (40), LIVE_INTERVAL (3s), LIVE_MAX_TOKENS (150).

set -e
cd "$(dirname "$0")"
ROOT="$(pwd)"
LOG_DIR="$ROOT/.run"
mkdir -p "$LOG_DIR"

# ── Colors ──────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
  C_OK=$'\033[32m'; C_INFO=$'\033[36m'; C_WARN=$'\033[33m'
  C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_END=$'\033[0m'
else
  C_OK=''; C_INFO=''; C_WARN=''; C_ERR=''; C_DIM=''; C_END=''
fi
say()  { echo "${C_INFO}→${C_END} $*"; }
ok()   { echo "${C_OK}✓${C_END} $*"; }
warn() { echo "${C_WARN}!${C_END} $*"; }
die()  { echo "${C_ERR}✗${C_END} $*" >&2; exit 1; }

# ── Config ──────────────────────────────────────────────────────────────────
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-8765}"
DASHBOARD_URL="http://localhost:${FRONTEND_PORT}/index.html"
HEALTH_URL="http://localhost:${BACKEND_PORT}/v1/health"

PYTHON_BIN="${PYTHON_BIN:-}"

# ── Pick a usable Python (3.11+) ────────────────────────────────────────────
pick_python() {
  if [ -n "$PYTHON_BIN" ] && [ -x "$PYTHON_BIN" ]; then return; fi
  for cand in python3.13 python3.12 python3.11; do
    if command -v "$cand" >/dev/null 2>&1; then PYTHON_BIN="$cand"; return; fi
  done
  if command -v python3 >/dev/null 2>&1; then
    local v
    v="$(python3 -c 'import sys; print(sys.version_info[:2])')"
    if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then
      PYTHON_BIN="python3"; return
    fi
    die "python3 is $v — need 3.11 or newer. Install with: brew install python@3.11"
  fi
  die "No python3 found. Install with: brew install python@3.11"
}

# ── Free a port if already bound (with user prompt) ─────────────────────────
free_port() {
  local port="$1" name="$2"
  local pids
  pids="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true)"
  if [ -z "$pids" ]; then return; fi
  warn "Port $port (for $name) is in use by PID(s): $pids"
  if [ "${FORCE:-}" = "1" ]; then
    say "FORCE=1 — killing"
    kill $pids 2>/dev/null || true
    sleep 1
    return
  fi
  read -rp "  Kill it and continue? [y/N] " ans
  if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
    kill $pids 2>/dev/null || true
    sleep 1
  else
    die "Port $port still occupied. Stop the other process or set FORCE=1."
  fi
}

# ── Setup the backend venv (idempotent) ─────────────────────────────────────
ensure_backend() {
  local venv="$ROOT/Argus/.venv"
  local pip="$venv/bin/pip"
  local py="$venv/bin/python"
  local marker="$venv/.deps-installed"
  local req="$ROOT/Argus/requirements.txt"

  if [ ! -x "$py" ]; then
    say "Creating Python venv at Argus/.venv ($PYTHON_BIN)"
    "$PYTHON_BIN" -m venv "$venv" || die "venv creation failed"
    "$py" -m pip install --upgrade pip >/dev/null 2>&1 || true
  fi

  # Reinstall only if requirements.txt is newer than our marker.
  if [ ! -f "$marker" ] || [ "$req" -nt "$marker" ]; then
    say "Installing Python dependencies (one-time, ~1 min)"
    "$pip" install -r "$req" 2>&1 | tail -5
    touch "$marker"
    ok "Backend dependencies installed"
  else
    ok "Backend dependencies up to date"
  fi
}

# ── Start backend, wait for /v1/health ──────────────────────────────────────
start_backend() {
  free_port "$BACKEND_PORT" "backend"
  say "Starting backend on :$BACKEND_PORT"
  (cd "$ROOT/Argus" && nohup .venv/bin/python -m uvicorn main:app --port "$BACKEND_PORT" \
       > "$LOG_DIR/backend.log" 2>&1 & echo $! > "$LOG_DIR/backend.pid")
  # Wait up to 30 s for health.
  local i=0
  until curl -sf "$HEALTH_URL" >/dev/null 2>&1; do
    i=$((i+1))
    if [ $i -gt 60 ]; then
      tail -20 "$LOG_DIR/backend.log" >&2
      die "Backend didn't come up in 30 s (see $LOG_DIR/backend.log)"
    fi
    sleep 0.5
  done
  ok "Backend up — $HEALTH_URL"
}

# ── Live traffic: 40 REAL Claude calls of varying complexity ────────────────
# Always --live. Needs a real key in Argus/.env (ANTHROPIC_API_KEY=sk-ant-...).
# No synthetic seed — the dashboard fills purely with real data.
LIVE_MAX_CALLS="${LIVE_MAX_CALLS:-40}"
LIVE_INTERVAL="${LIVE_INTERVAL:-3}"
LIVE_MAX_TOKENS="${LIVE_MAX_TOKENS:-150}"

run_live_traffic() {
  # Hard requirement: a real key. Without it we do NOT fall back to synthetic
  # (the whole point is live data) — we warn and skip, leaving the dashboard
  # empty until a key is provided.
  if ! (cd "$ROOT/Argus" && .venv/bin/python - <<'PY'
import os, sys
key = os.getenv("ANTHROPIC_API_KEY")
if not key and os.path.exists(".env"):
    for line in open(".env", encoding="utf-8"):
        if line.strip().startswith("ANTHROPIC_API_KEY="):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")
sys.exit(0 if (key and key != "sk-ant-your-key-here") else 1)
PY
  ); then
    warn "No Anthropic key found — set ANTHROPIC_API_KEY in Argus/.env"
    warn "Skipping live traffic. Dashboard will be empty until you add a key and rerun."
    return
  fi

  say "Starting LIVE traffic — $LIVE_MAX_CALLS REAL Claude calls of varying complexity (spends money)"
  (cd "$ROOT/Argus" && nohup .venv/bin/python -u live_traffic.py \
       --live --max-calls "$LIVE_MAX_CALLS" --interval "$LIVE_INTERVAL" --max-tokens "$LIVE_MAX_TOKENS" \
       > "$LOG_DIR/traffic.log" 2>&1 & echo $! > "$LOG_DIR/traffic.pid")
  sleep 1
  ok "Live traffic running ($LIVE_MAX_CALLS calls @ ~${LIVE_INTERVAL}s, max ${LIVE_MAX_TOKENS} out-tok/call)"
}

# ── Static frontend ─────────────────────────────────────────────────────────
start_frontend() {
  free_port "$FRONTEND_PORT" "frontend"
  say "Starting frontend on :$FRONTEND_PORT"
  (cd "$ROOT" && nohup "$PYTHON_BIN" -m http.server "$FRONTEND_PORT" \
       > "$LOG_DIR/frontend.log" 2>&1 & echo $! > "$LOG_DIR/frontend.pid")
  sleep 1
  ok "Frontend up — $DASHBOARD_URL"
}

# ── Open the browser ────────────────────────────────────────────────────────
open_dashboard() {
  if command -v open >/dev/null 2>&1; then
    open "$DASHBOARD_URL"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$DASHBOARD_URL" >/dev/null 2>&1 &
  else
    warn "Couldn't auto-open browser — visit $DASHBOARD_URL"
  fi
}

# ── Ctrl-C → stop everything cleanly ────────────────────────────────────────
cleanup() {
  echo
  say "Stopping…"
  for p in traffic backend frontend; do
    local pidfile="$LOG_DIR/$p.pid"
    if [ -f "$pidfile" ]; then
      local pid; pid="$(cat "$pidfile")"
      if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        # Give it a sec, then SIGKILL if still alive.
        sleep 0.5
        kill -9 "$pid" 2>/dev/null || true
      fi
      rm -f "$pidfile"
    fi
  done
  ok "All stopped"
  exit 0
}
trap cleanup INT TERM

# ── Go ──────────────────────────────────────────────────────────────────────
echo
echo "${C_INFO}═══ Argus launcher ═══${C_END}"
pick_python
ok "Python: $($PYTHON_BIN --version)"
ensure_backend
start_backend
run_live_traffic
start_frontend
open_dashboard

echo
echo "${C_OK}═══════════════════════════════════════════${C_END}"
echo "${C_OK}  Dashboard:${C_END} $DASHBOARD_URL"
echo "${C_OK}  Backend:  ${C_END} http://localhost:$BACKEND_PORT"
echo "${C_OK}  Logs:     ${C_END} $LOG_DIR/"
echo "${C_OK}═══════════════════════════════════════════${C_END}"
echo "${C_DIM}Tailing traffic — press Ctrl-C to stop everything${C_END}"
echo
# Tail the most-interesting log so the user sees activity. Detach if it dies.
tail -f "$LOG_DIR/traffic.log" 2>/dev/null || true
wait
