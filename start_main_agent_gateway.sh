#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  source "$ROOT_DIR/.env"
  set +a
fi

: "${MAIN_AGENT_GATEWAY_PORT:=4011}"

find_listener_pid() {
  if ! command -v lsof >/dev/null 2>&1; then
    return 1
  fi

  lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -n 1
}

if PID_ON_PORT="$(find_listener_pid "$MAIN_AGENT_GATEWAY_PORT")" && [[ -n "$PID_ON_PORT" ]]; then
  echo "port $MAIN_AGENT_GATEWAY_PORT already in use by pid $PID_ON_PORT" >&2
  exit 70
fi

PYTHON_BIN="python3"
if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
fi

exec "$PYTHON_BIN" "$ROOT_DIR/main_agent_gateway.py"
