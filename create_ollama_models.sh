#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  source "$ROOT_DIR/.env"
  set +a
fi

ollama create uncensored-fallback -f "$ROOT_DIR/Modelfile.huihui-qwen3.5-27b"
ollama create gemma-fallback -f "$ROOT_DIR/Modelfile.gemma-4-26b"
