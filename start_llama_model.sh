#!/bin/zsh
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <model-name> <port>" >&2
  exit 1
fi

MODEL_NAME="$1"
PORT="$2"
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  source "$ROOT_DIR/.env"
  set +a
fi

API_KEY="${LOCAL_LLAMA_API_KEY:-sk-local-llama}"
LOCAL_LLAMA_SERVER="$ROOT_DIR/../llama.cpp-latest/build/bin/llama-server"
LLAMA_SERVER_BIN="${LLAMA_SERVER_BIN:-}"

find_listener_pid() {
  if ! command -v lsof >/dev/null 2>&1; then
    return 1
  fi

  lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -n 1
}

if [[ -z "$LLAMA_SERVER_BIN" && -x "$LOCAL_LLAMA_SERVER" ]]; then
  LLAMA_SERVER_BIN="$LOCAL_LLAMA_SERVER"
fi

if [[ -z "$LLAMA_SERVER_BIN" ]]; then
  LLAMA_SERVER_BIN="llama-server"
fi

MODEL_PATH=""
MMPROJ_PATH=""
CTX_SIZE=""
PARALLEL=""
EXTRA_ARGS=()

case "$MODEL_NAME" in
  qwen3.5-27b|qwen-deep|qwen-deep-vl)
    MODEL_PATH="/Users/zyh/.lmstudio/models/Jackrong/Qwen3.5-27B-Claude-4.6-Opus-Reasoning-Distilled-v2-GGUF/Qwen3.5-27B.Q8_0.gguf"
    MMPROJ_PATH="/Users/zyh/.lmstudio/models/Jackrong/Qwen3.5-27B-Claude-4.6-Opus-Reasoning-Distilled-v2-GGUF/mmproj-BF16.gguf"
    CTX_SIZE="65536"
    PARALLEL="1"
    EXTRA_ARGS+=(--temp 0.6 --top-p 0.95 --top-k 20 --min-p 0.0 --reasoning-format deepseek)
    ;;
  qwen3.5-9b|qwen-fast|qwen-fast-vl)
    MODEL_PATH="/Users/zyh/.lmstudio/models/Jackrong/Qwen3.5-9B-Claude-4.6-Opus-Reasoning-Distilled-v2-GGUF/Qwen3.5-9B.Q8_0.gguf"
    MMPROJ_PATH="/Users/zyh/.lmstudio/models/Jackrong/Qwen3.5-9B-Claude-4.6-Opus-Reasoning-Distilled-v2-GGUF/mmproj-BF16.gguf"
    CTX_SIZE="32768"
    PARALLEL="1"
    EXTRA_ARGS+=(--temp 0.6 --top-p 0.95 --top-k 20 --min-p 0.0 --reasoning-format deepseek)
    ;;
  qwen2.5-1.5b|qwen-extract)
    MODEL_PATH="/Users/zyh/.lmstudio/models/Qwen/Qwen2.5-1.5B-Instruct-GGUF/qwen2.5-1.5b-instruct-q5_k_m.gguf"
    CTX_SIZE="32768"
    PARALLEL="1"
    ;;
  omnicoder-9b|code-fast)
    MODEL_PATH="/Users/zyh/.lmstudio/models/Tesslate/OmniCoder-9B-GGUF/omnicoder-9b-q8_0.gguf"
    CTX_SIZE="32768"
    PARALLEL="1"
    EXTRA_ARGS+=(--temp 0.6 --top-p 0.95 --top-k 20)
    ;;
  huihui-27b)
    MODEL_PATH="/Users/zyh/.lmstudio/models/cs2764/Huihui-Qwen3.5-27B-Claude-4.6-Opus-abliterated-Q8_0-GGUF/huihui-qwen3.5-27b-claude-4.6-opus-abliterated-q8_0.gguf"
    CTX_SIZE="65536"
    PARALLEL="1"
    EXTRA_ARGS+=(--temp 0.7 --top-p 0.95 --top-k 20 --min-p 0.0)
    ;;
  gemma-4-26b)
    MODEL_PATH="/Users/zyh/.lmstudio/models/lmstudio-community/gemma-4-26B-A4B-it-GGUF/gemma-4-26B-A4B-it-Q8_0.gguf"
    MMPROJ_PATH="/Users/zyh/.lmstudio/models/lmstudio-community/gemma-4-26B-A4B-it-GGUF/mmproj-gemma-4-26B-A4B-it-BF16.gguf"
    CTX_SIZE="65536"
    PARALLEL="1"
    EXTRA_ARGS+=(--temp 0.7 --top-p 0.95 --top-k 64)
    ;;
  embed-m3)
    MODEL_PATH="/Users/zyh/.lmstudio/models/stefancosma/bge-m3-Q4_K_M-GGUF/bge-m3-q4_k_m.gguf"
    CTX_SIZE="8192"
    PARALLEL="4"
    EXTRA_ARGS+=(--embedding)
    ;;
  *)
    echo "unknown model: $MODEL_NAME" >&2
    exit 1
    ;;
esac

if [[ ! -r "$MODEL_PATH" ]]; then
  echo "model file not found: $MODEL_PATH" >&2
  exit 66
fi

if [[ -n "$MMPROJ_PATH" && ! -r "$MMPROJ_PATH" ]]; then
  echo "mmproj file not found: $MMPROJ_PATH" >&2
  exit 66
fi

if [[ -x "$LLAMA_SERVER_BIN" ]]; then
  :
elif ! command -v "$LLAMA_SERVER_BIN" >/dev/null 2>&1; then
  echo "llama-server binary not found: $LLAMA_SERVER_BIN" >&2
  exit 69
fi

if PID_ON_PORT="$(find_listener_pid "$PORT")" && [[ -n "$PID_ON_PORT" ]]; then
  echo "port $PORT already in use by pid $PID_ON_PORT" >&2
  exit 70
fi

CMD=(
  "$LLAMA_SERVER_BIN"
  --host 127.0.0.1
  --port "$PORT"
  --api-key "$API_KEY"
  --model "$MODEL_PATH"
  --alias "$MODEL_NAME"
  --ctx-size "$CTX_SIZE"
  --parallel "$PARALLEL"
  --jinja
  --metrics
  --slots
  --no-webui
)

if [[ -n "$MMPROJ_PATH" ]]; then
  CMD+=(--mmproj "$MMPROJ_PATH")
fi

CMD+=("${EXTRA_ARGS[@]}")

exec "${CMD[@]}"
