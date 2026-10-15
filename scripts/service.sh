#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="$PROJECT_DIR/.super-partner/runtime"
LOG_DIR="$PROJECT_DIR/.super-partner/logs"
API_PID_FILE="$RUNTIME_DIR/api.pid"
WEB_PID_FILE="$RUNTIME_DIR/web.pid"
API_URL="http://127.0.0.1:8000"
WEB_URL="http://127.0.0.1:5173"
UNIT_PREFIX="super-partner-$(printf '%s' "$PROJECT_DIR" | cksum | cut -d ' ' -f 1)"
USE_SYSTEMD=0
if command -v systemd-run >/dev/null 2>&1 && command -v systemctl >/dev/null 2>&1 \
  && systemctl --user show-environment >/dev/null 2>&1; then
  USE_SYSTEMD=1
fi

mkdir -p "$RUNTIME_DIR" "$LOG_DIR"

unit_for() {
  if [[ "$1" == "$API_PID_FILE" ]]; then
    echo "$UNIT_PREFIX-api.service"
  else
    echo "$UNIT_PREFIX-web.service"
  fi
}

is_running() {
  local pid_file="$1"
  if [[ "$USE_SYSTEMD" -eq 1 ]]; then
    local unit state
    unit="$(unit_for "$pid_file")"
    state="$(systemctl --user show "$unit" --property=ActiveState --value 2>/dev/null || true)"
    if [[ "$state" == "active" || "$state" == "activating" || "$state" == "reloading" ]]; then
      systemctl --user show "$unit" --property=MainPID --value >"$pid_file"
      return 0
    fi
  fi
  [[ -f "$pid_file" ]] || return 1
  local pid
  pid="$(<"$pid_file")"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

launch_service() {
  local pid_file="$1" log_file="$2"
  shift 2
  if [[ "$USE_SYSTEMD" -eq 1 ]]; then
    local unit name
    local -a inherited=() unset_proxy=()
    unit="$(unit_for "$pid_file")"
    # Preserve configured channels and local tools without placing credential values in
    # command arguments. --setenv=NAME reads the value from systemd-run's environment.
    while IFS= read -r name; do
      case "$name" in
        PATH|LANG|LC_*|DISPLAY|WAYLAND_DISPLAY|XAUTHORITY|DBUS_SESSION_BUS_ADDRESS|XDG_RUNTIME_DIR|SSH_AUTH_SOCK|SUPER_PARTNER_*|*_API_KEY|GITHUB_TOKEN|HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY|http_proxy|https_proxy|all_proxy|no_proxy|VCS_HOME|VERDI_HOME|SNPSLMD_LICENSE_FILE|LM_LICENSE_FILE|LD_LIBRARY_PATH)
          inherited+=("--setenv=$name") ;;
      esac
    done < <(compgen -e)
    # The desktop manager may retain lowercase proxy variables that are absent from
    # this shell. Do not let those override the caller's uppercase proxy settings.
    for name in HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy; do
      if [[ ! -v "$name" ]]; then unset_proxy+=("$name"); fi
    done
    if [[ "${#unset_proxy[@]}" -gt 0 ]]; then
      inherited+=("--property=UnsetEnvironment=${unset_proxy[*]}")
    fi
    systemd-run --user --quiet --collect --unit="$unit" \
      --property=Type=exec --property="WorkingDirectory=$PROJECT_DIR" \
      --property=Restart=on-failure --property=RestartSec=2 \
      --property="StandardOutput=append:$log_file" \
      --property="StandardError=append:$log_file" \
      "${inherited[@]}" -- "$@"
    systemctl --user show "$unit" --property=MainPID --value >"$pid_file"
  else
    # For hosts without a user service manager, at least detach the process's session.
    local -a detached=()
    if command -v setsid >/dev/null 2>&1; then detached=(setsid); fi
    (cd "$PROJECT_DIR"; exec nohup "${detached[@]}" "$@") >>"$log_file" 2>&1 </dev/null &
    echo "$!" >"$pid_file"
  fi
}

prepare() {
  if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    echo "[setup] Creating Python environment..."
    python3 -m venv "$PROJECT_DIR/.venv"
  fi
  if ! "$PROJECT_DIR/.venv/bin/python" -c 'import fastapi, openpyxl, super_partner' 2>/dev/null; then
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
    launch_service "$API_PID_FILE" "$LOG_DIR/api.log" /usr/bin/env \
      SUPER_PARTNER_DATA_DIR="$PROJECT_DIR/.super-partner" \
      SUPER_PARTNER_WORKSPACE_ROOT="$PROJECT_DIR/workspaces" \
      "$PROJECT_DIR/.venv/bin/uvicorn" super_partner.api:app \
      --app-dir "$PROJECT_DIR" --host 127.0.0.1 --port 8000
    api_started=1
  fi
  if ! is_running "$WEB_PID_FILE"; then
    echo "[start] Starting web console..."
    launch_service "$WEB_PID_FILE" "$LOG_DIR/web.log" \
      "$PROJECT_DIR/frontend/node_modules/.bin/vite" "$PROJECT_DIR/frontend" \
      --host 127.0.0.1 --port 5173 --strictPort
    web_started=1
  fi

  local ready=0
  local deadline=$((SECONDS + 60))
  while (( SECONDS < deadline )); do
    if ! is_running "$API_PID_FILE" || ! is_running "$WEB_PID_FILE"; then
      break
    fi
    if curl --noproxy '*' --connect-timeout 1 --max-time 2 \
      --silent --fail "$API_URL/health" >/dev/null 2>&1 \
      && curl --noproxy '*' --connect-timeout 1 --max-time 2 \
        --silent --fail "$WEB_URL" >/dev/null 2>&1; then
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
  local unit
  unit="$(unit_for "$pid_file")"
  if [[ "$USE_SYSTEMD" -eq 1 ]] && [[ "$(systemctl --user show "$unit" --property=LoadState --value 2>/dev/null || true)" == "loaded" ]]; then
    echo "[stop] Stopping $name ($unit)..."
    systemctl --user stop "$unit"
  elif is_running "$pid_file"; then
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
