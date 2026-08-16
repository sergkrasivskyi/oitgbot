#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
ENV_FILE="${OITGBOT_ENV_FILE:-$HOME/.config/oitgbot/env}"

[[ -d "$PROJECT_DIR" ]] || { echo "Project directory is missing: $PROJECT_DIR" >&2; exit 1; }
cd "$PROJECT_DIR"
[[ -x "$VENV_PYTHON" ]] || { echo "Virtualenv missing; run setup-ubuntu.sh first." >&2; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "External env file is missing: $ENV_FILE" >&2; exit 1; }

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

for required in BOT_TOKEN ALL_CHANNEL_ID PROP_CHANNEL_ID; do
  [[ -n "${!required:-}" ]] || { echo "Missing required environment variable: $required" >&2; exit 1; }
done

PYTHONPATH="$PROJECT_DIR" "$VENV_PYTHON" -c 'import oitgbot.app; print("Application import: OK")'
PYTHONPATH="$PROJECT_DIR" "$VENV_PYTHON" - <<'PY'
import os
from pathlib import Path
from oitgbot.config import settings

for label, value in (("bot log", settings.log_file), ("rolling log", settings.rolling_oi_log_file), ("rolling state", settings.rolling_oi_signal_state_file)):
    parent = Path(value).expanduser().parent
    if not parent.exists() or not os.access(parent, os.W_OK | os.X_OK):
        raise SystemExit(f"{label} parent is not writable: {parent}")
    print(f"{label} parent writable: {parent}")
PY

shared_storage_available=0
for shared_path in /sdcard/Download /storage/emulated/0/Download; do
  if [[ -d "$shared_path" && -w "$shared_path" ]]; then
    echo "Android Downloads available: $shared_path"
    shared_storage_available=1
    break
  fi
done
if [[ "$shared_storage_available" -ne 1 ]]; then
  echo "Android Downloads is not currently writable; make Termux shared storage available before log export." >&2
  exit 1
fi

echo "Timezone: ${TZ:-Europe/Kyiv} (application timestamps remain UTC where defined)"
echo "Git branch: $(git -C "$PROJECT_DIR" branch --show-current)"
echo "Git commit: $(git -C "$PROJECT_DIR" rev-parse --short HEAD)"
echo "Git status:"
git -C "$PROJECT_DIR" status --short
echo "Preflight complete; no network, Telegram, or state actions were performed."
