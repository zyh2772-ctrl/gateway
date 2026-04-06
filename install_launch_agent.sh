#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$ROOT_DIR/launchd/com.zyh.local-llm-stack.supervisor.plist.template"
PROFILE="${1:-large-fallback}"
AGENT_DIR="$HOME/Library/LaunchAgents"
TARGET="$AGENT_DIR/com.zyh.local-llm-stack.supervisor.plist"
RUNTIME_DIR="$ROOT_DIR/runtime"
STDOUT_LOG="$RUNTIME_DIR/launchd.stdout.log"
STDERR_LOG="$RUNTIME_DIR/launchd.stderr.log"

mkdir -p "$AGENT_DIR" "$RUNTIME_DIR"

if [[ ! -r "$TEMPLATE" ]]; then
  echo "missing template: $TEMPLATE" >&2
  exit 66
fi

python3 - "$TEMPLATE" "$TARGET" "$ROOT_DIR/start_stack_supervisor.sh" "$PROFILE" "$ROOT_DIR" "$STDOUT_LOG" "$STDERR_LOG" <<'PY'
from pathlib import Path
import sys

template = Path(sys.argv[1]).read_text(encoding="utf-8")
rendered = (
    template
    .replace("{{START_SCRIPT}}", sys.argv[3])
    .replace("{{PROFILE}}", sys.argv[4])
    .replace("{{WORKDIR}}", sys.argv[5])
    .replace("{{STDOUT_LOG}}", sys.argv[6])
    .replace("{{STDERR_LOG}}", sys.argv[7])
)
Path(sys.argv[2]).write_text(rendered, encoding="utf-8")
PY

echo "launch agent written to: $TARGET"
echo "next commands:"
echo "  launchctl unload $TARGET 2>/dev/null || true"
echo "  launchctl load $TARGET"
echo "  launchctl start com.zyh.local-llm-stack.supervisor"
