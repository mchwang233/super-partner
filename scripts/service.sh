#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="$PROJECT_DIR/.super-partner/runtime"
LOG_DIR="$PROJECT_DIR/.super-partner/logs"
API_PID_FILE="$RUNTIME_DIR/api.pid"
WEB_PID_FILE="$RUNTIME_DIR/web.pid"
API_URL="http://127.0.0.1:8000"
WEB_URL="http://127.0.0.1:5173"

mkdir -p "$RUNTIME_DIR" "$LOG_DIR"

is_running() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] || return 1
  local pid
  pid="$(<"$pid_file")"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

prepare() {
  if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    echo "[setup] Creating Python environment..."
    python3 -m venv "$PROJECT_DIR/.venv"
  fi
  if ! "$PROJECT_DIR/.venv/bin/python" -c 'import fastapi, super_partner' 2>/dev/null; then
    echo "[setup] Installing backend dependencies..."
    "$PROJECT_DIR/.venv/bin/python" -m pip install -e "$PROJECT_DIR"
  fi
  if [[ ! -x "$PROJECT_DIR/frontend/node_modules/.bin/vite" ]]; then
    echo "[setup] Installing frontend dependencies..."
    npm install --prefix "$PROJECT_DIR/frontend"
  fi
}

start() {
  local api_started=0
  local web_started=0
  prepare
  if ! is_running "$API_PID_FILE"; then
    echo "[start] Starting API..."
    nohup env \
      SUPER_PARTNER_DATA_DIR="$PROJECT_DIR/.super-partner" \
      SUPER_PARTNER_WORKSPACE_ROOT="$PROJECT_DIR/workspaces" \
      "$PROJECT_DIR/.venv/bin/uvicorn" super_partner.api:app \
      --app-dir "$PROJECT_DIR" --host 127.0.0.1 --port 8000 \
      >"$LOG_DIR/api.log" 2>&1 </dev/null &
    echo "$!" >"$API_PID_FILE"
    api_started=1
  fi
  if ! is_running "$WEB_PID_FILE"; then
    echo "[start] Starting web console..."
    nohup npm --prefix "$PROJECT_DIR/frontend" run dev -- --host 127.0.0.1 \
      >"$LOG_DIR/web.log" 2>&1 </dev/null &
    echo "$!" >"$WEB_PID_FILE"
    web_started=1
  fi

  local ready=0
  for _ in {1..60}; do
    if curl --silent --fail "$API_URL/health" >/dev/null 2>&1 \
      && curl --silent --fail "$WEB_URL" >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 0.25
  done
  if [[ "$ready" -ne 1 ]]; then
    echo "[error] Services did not become healthy. See $LOG_DIR"
    [[ "$web_started" -eq 1 ]] && stop_process "web console" "$WEB_PID_FILE"
    [[ "$api_started" -eq 1 ]] && stop_process "API" "$API_PID_FILE"
    exit 1
  fi
  echo
  echo "Super Partner is ready: $WEB_URL"
  echo "API: $API_URL"
  echo "Logs: $LOG_DIR"
  echo "Stop: ./stop.sh"
}

stop_process() {
  local name="$1"
  local pid_file="$2"
  if is_running "$pid_file"; then
    local pid
    pid="$(<"$pid_file")"
    echo "[stop] Stopping $name ($pid)..."
    kill "$pid"
    for _ in {1..20}; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.1
    done
  fi
  rm -f "$pid_file"
}

stop() {
  stop_process "web console" "$WEB_PID_FILE"
  stop_process "API" "$API_PID_FILE"
  echo "Super Partner stopped."
}

status() {
  if is_running "$API_PID_FILE"; then echo "API: running ($( <"$API_PID_FILE"))"; else echo "API: stopped"; fi
  if is_running "$WEB_PID_FILE"; then echo "Web: running ($( <"$WEB_PID_FILE"))"; else echo "Web: stopped"; fi
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  *) echo "Usage: $0 {start|stop|restart|status}"; exit 2 ;;
esac
