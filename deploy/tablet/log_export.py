#!/usr/bin/env python3
"""Create a safe, small diagnostic ZIP for the Android tablet release."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterable


EXPORT_DIRECTORY_NAME = "OI-bot-logs"
DEFAULT_SHARED_BASES = (
    Path("/sdcard/Download"),
    Path("/storage/emulated/0/Download"),
)
LOG_FILES = ("bot.log", "rolling_oi.log")
STATE_FILES = ("rolling_oi_signal_state.json",)


def timestamp_name(now: datetime | None = None) -> str:
    """Return the timestamp component used in an uploadable archive name."""
    return (now or datetime.now().astimezone()).strftime("%Y%m%d-%H%M%S")


def choose_export_dir(shared_bases: Iterable[Path]) -> Path:
    """Choose the first existing writable Android Downloads directory.

    The directory is deliberately created only after its parent has been found
    writable. This preserves the clear primary/fallback policy without ever
    assuming Android shared storage is mounted inside proot.
    """
    for base in shared_bases:
        if base.is_dir() and os.access(base, os.W_OK | os.X_OK):
            export_dir = base / EXPORT_DIRECTORY_NAME
            export_dir.mkdir(mode=0o700, exist_ok=True)
            if os.access(export_dir, os.W_OK | os.X_OK):
                return export_dir
    candidates = ", ".join(str(path) for path in shared_bases)
    raise RuntimeError(
        "No writable Android Downloads path was found. In Termux/proot, make "
        f"shared storage available, then retry. Checked: {candidates}"
    )


def selected_files(project_root: Path) -> list[Path]:
    """Return active logs, every existing rotation, and signal state."""
    selected: list[Path] = []
    for name in LOG_FILES:
        active = project_root / name
        if active.is_file():
            selected.append(active)
        selected.extend(
            path
            for path in sorted(project_root.glob(f"{name}.*"))
            if path.is_file()
        )
    selected.extend(
        project_root / name
        for name in STATE_FILES
        if (project_root / name).is_file()
    )
    return selected


def _command_output(command: list[str], project_root: Path) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=project_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return "unavailable"
    output = result.stdout.strip()
    return output if output else "unavailable"


def _bot_process_count(project_root: Path) -> str:
    """Report a count instead of process command lines, which may contain secrets."""
    try:
        result = subprocess.run(
            ["pgrep", "-fc", "python.*run.py"],
            cwd=project_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return "unavailable"
    return result.stdout.strip() or "0"


def runtime_info(project_root: Path, exported_at: datetime | None = None) -> str:
    """Build diagnostic metadata without reading or printing environment secrets."""
    now = exported_at or datetime.now().astimezone()
    lines = [
        f"Export timestamp: {now.isoformat()}",
        f"Timezone: {time.tzname[0] if time.tzname else 'unavailable'}",
        f"Git branch: {_command_output(['git', 'branch', '--show-current'], project_root)}",
        f"Git commit: {_command_output(['git', 'rev-parse', 'HEAD'], project_root)}",
        "Git status:",
        _command_output(['git', 'status', '--short'], project_root),
        f"Python version: {sys.version.replace(os.linesep, ' ')}",
        f"Project path: {project_root.resolve()}",
        f"Hostname: {socket.gethostname()}",
        f"Bot process count (python run.py): {_bot_process_count(project_root)}",
        "Diagnostic file sizes:",
    ]
    for path in selected_files(project_root):
        lines.append(f"{path.name}: {path.stat().st_size} bytes")
    state_file = project_root / "rolling_oi_signal_state.json"
    lines.append(f"Rolling state exists: {'yes' if state_file.is_file() else 'no'}")
    usage = shutil.disk_usage(project_root)
    lines.append(f"Disk free: {usage.free} bytes of {usage.total} bytes")
    return "\n".join(lines) + "\n"


def _archive_path(export_dir: Path, stamp: str) -> Path:
    candidate = export_dir / f"oi-bot-logs-{stamp}.zip"
    suffix = 1
    while candidate.exists():
        candidate = export_dir / f"oi-bot-logs-{stamp}-{suffix}.zip"
        suffix += 1
    return candidate


def create_archive(
    project_root: Path, export_dir: Path, exported_at: datetime | None = None
) -> Path:
    """Copy live diagnostics to staging, then ZIP only those staged snapshots."""
    project_root = project_root.resolve()
    export_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = exported_at or datetime.now().astimezone()
    archive = _archive_path(export_dir, timestamp_name(now))
    with tempfile.TemporaryDirectory(prefix="oitgbot-log-export-") as temporary:
        staging = Path(temporary)
        for source in selected_files(project_root):
            shutil.copy2(source, staging / source.name)
        (staging / "runtime_info.txt").write_text(runtime_info(project_root, now), encoding="utf-8")
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
            for source in sorted(staging.iterdir()):
                zip_file.write(source, arcname=source.name)
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--export-dir", type=Path)
    parser.add_argument("--android-downloads", action="store_true")
    args = parser.parse_args()
    try:
        export_dir = args.export_dir
        if args.android_downloads:
            export_dir = choose_export_dir(DEFAULT_SHARED_BASES)
        if export_dir is None:
            parser.error("provide --export-dir or --android-downloads")
        print(create_archive(args.project_root, export_dir))
    except RuntimeError as error:
        print(f"Log export failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
