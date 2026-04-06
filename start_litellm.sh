#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  source "$ROOT_DIR/.env"
  set +a
fi

: "${LITELLM_PORT:=4000}"

find_listener_pid() {
  if ! command -v lsof >/dev/null 2>&1; then
    return 1
  fi

  lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -n 1
}

if PID_ON_PORT="$(find_listener_pid "$LITELLM_PORT")" && [[ -n "$PID_ON_PORT" ]]; then
  echo "port $LITELLM_PORT already in use by pid $PID_ON_PORT" >&2
  exit 70
fi

if [[ ! -r "$ROOT_DIR/litellm.config.yaml" ]]; then
  echo "missing LiteLLM config: $ROOT_DIR/litellm.config.yaml" >&2
  exit 66
fi

if [[ -x "$ROOT_DIR/.venv/bin/litellm" ]]; then
  exec "$ROOT_DIR/.venv/bin/litellm" \
    --config "$ROOT_DIR/litellm.config.yaml" \
    --port "$LITELLM_PORT"
fi

if command -v litellm >/dev/null 2>&1; then
  exec litellm \
    --config "$ROOT_DIR/litellm.config.yaml" \
    --port "$LITELLM_PORT"
fi

LITELLM_REPO="$ROOT_DIR/../litellm-main"

cd "$LITELLM_REPO"
exec python3 litellm/proxy/proxy_cli.py \
  --config "$ROOT_DIR/litellm.config.yaml" \
  --port "$LITELLM_PORT"
