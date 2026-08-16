#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"

[[ -x "$VENV_PYTHON" ]] || { echo "Virtualenv missing; run setup-ubuntu.sh first." >&2; exit 1; }
exec "$VENV_PYTHON" "$SCRIPT_DIR/log_export.py" --project-root "$PROJECT_DIR" --android-downloads
