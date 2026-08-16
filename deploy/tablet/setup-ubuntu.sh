#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"

cd "$PROJECT_DIR"
command -v python3 >/dev/null || { echo "Python 3 is required." >&2; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3, 10), sys.version'

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"
mkdir -p "$PROJECT_DIR/state"
chmod +x "$SCRIPT_DIR"/*.sh "$SCRIPT_DIR/log_export.py"

TZ="${TZ:-Europe/Kyiv}" PYTHONPATH="$PROJECT_DIR" "$VENV_DIR/bin/python" -c \
  'import oitgbot.app; print("Application import: OK")'
echo "Timezone for tablet runs: ${TZ:-Europe/Kyiv} (set TZ in the external env file)."
echo "Setup complete. Create or preserve secrets at ~/.config/oitgbot/env, then run check-ubuntu.sh."
