#!/usr/bin/env bash
# run_bridge.sh - launch the Telegram coach bridge (persistent poller).
# Run by hand for testing, or from systemd / nohup. A localhost port lock inside
# the Python process guarantees only one instance ever polls.
#
# Paths are resolved RELATIVE to this script, so the repo can live anywhere.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo root (parent of scripts/)
SCRIPT="$ROOT/telegram_bridge.py"

# Prefer a local virtualenv if present, else fall back to python3 on PATH.
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

export PYTHONIOENCODING="utf-8"
export PYTHONUTF8="1"

# --- Optional: pin the Copilot CLI model + reasoning effort (else CLI defaults). ---
# Usual place for these is .env, but you can also uncomment here:
# export AGBOT_MODEL="gpt-5.6-luna"    # any id from `/model`, or "auto"
# export AGBOT_REASONING_EFFORT="max"  # none|minimal|low|medium|high|xhigh|max
# export AGBOT_LLM_TIMEOUT="600"       # raise for slow high-reasoning models

# Load .env (simple KEY=VALUE lines) if present.
if [ -f "$ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ROOT/.env"
    set +a
fi

echo "Starting coach bridge: $SCRIPT"
exec "$PY" "$SCRIPT"
