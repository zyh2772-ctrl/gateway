#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCK_PATH="$ROOT_DIR/runtime/supervisor.lock"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  source "$ROOT_DIR/.env"
  set +a
fi

if [[ -f "$LOCK_PATH" ]] && ! lsof "$LOCK_PATH" >/dev/null 2>&1; then
  rm -f "$LOCK_PATH"
fi

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  exec "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/stack_supervisor.py" run "$@"
fi

exec python3 "$ROOT_DIR/stack_supervisor.py" run "$@"
