#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  source "$ROOT_DIR/.env"
  set +a
fi

: "${LLAMA_CPP_PORT:=18080}"
: "${LOCAL_LLAMA_API_KEY:=sk-local-llama}"
: "${LLAMA_MODELS_MAX:=1}"
: "${LLAMA_SLEEP_IDLE_SECONDS:=900}"

exec llama-server \
  --host 127.0.0.1 \
  --port "$LLAMA_CPP_PORT" \
  --api-key "$LOCAL_LLAMA_API_KEY" \
  --models-preset "$ROOT_DIR/llama-models.ini" \
  --models-max "$LLAMA_MODELS_MAX" \
  --models-autoload \
  --sleep-idle-seconds "$LLAMA_SLEEP_IDLE_SECONDS" \
  --jinja \
  --reasoning-format deepseek \
  --metrics \
  --slots \
  --no-webui
