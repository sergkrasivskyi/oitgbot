#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
ENV_FILE="${OITGBOT_ENV_FILE:-$HOME/.config/oitgbot/env}"

[[ -x "$VENV_PYTHON" ]] || { echo "Virtualenv missing; run setup-ubuntu.sh first." >&2; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "External env file is missing: $ENV_FILE" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

export TZ="${TZ:-Europe/Kyiv}"
export ROLLING_OI_CADENCE_SECONDS="${ROLLING_OI_CADENCE_SECONDS:-30}"
export ROLLING_OI_WORKERS="${ROLLING_OI_WORKERS:-20}"
export ROLLING_OI_5M_TRIGGER_PCT="${ROLLING_OI_5M_TRIGGER_PCT:-5}"
export ROLLING_OI_5M_REARM_PCT="${ROLLING_OI_5M_REARM_PCT:-3}"

cd "$PROJECT_DIR"
exec "$VENV_PYTHON" run.py
