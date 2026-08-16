from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from deploy.tablet import log_export


class TabletLogExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "bot.log").write_text("bot snapshot", encoding="utf-8")
        (self.project / "bot.log.1").write_text("bot rotation", encoding="utf-8")
        (self.project / "rolling_oi.log").write_text("rolling snapshot", encoding="utf-8")
        (self.project / "rolling_oi_signal_state.json").write_text("{}", encoding="utf-8")
        (self.project / ".env").write_text("BOT_TOKEN=never-export", encoding="utf-8")
        (self.project / ".git").mkdir()
        self.export_dir = self.root / "exports"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_selects_only_expected_diagnostics(self) -> None:
        self.assertEqual(
            [path.name for path in log_export.selected_files(self.project)],
            ["bot.log", "bot.log.1", "rolling_oi.log", "rolling_oi_signal_state.json"],
        )

    def test_archive_is_timestamped_and_copies_live_files(self) -> None:
        stamp = datetime(2026, 8, 16, 10, 30, 5, tzinfo=timezone.utc)
        archive = log_export.create_archive(self.project, self.export_dir, stamp)
        self.assertEqual(archive.name, "oi-bot-logs-20260816-103005.zip")
        self.assertTrue((self.project / "bot.log").is_file())
        with zipfile.ZipFile(archive) as zip_file:
            self.assertEqual(
                sorted(zip_file.namelist()),
                ["bot.log", "bot.log.1", "rolling_oi.log", "rolling_oi_signal_state.json", "runtime_info.txt"],
            )
            self.assertNotIn(".env", zip_file.namelist())
            self.assertNotIn("never-export", zip_file.read("runtime_info.txt").decode())

    def test_missing_optional_state_file_does_not_block_export(self) -> None:
        (self.project / "rolling_oi_signal_state.json").unlink()
        archive = log_export.create_archive(self.project, self.export_dir)
        self.assertTrue(archive.is_file())

    def test_fallback_downloads_path_is_used(self) -> None:
        fallback = self.root / "fallback"
        fallback.mkdir()
        selected = log_export.choose_export_dir([self.root / "missing", fallback])
        self.assertEqual(selected, fallback / "OI-bot-logs")
        self.assertTrue(selected.is_dir())

    def test_missing_downloads_paths_has_actionable_error(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "No writable Android Downloads path"):
            log_export.choose_export_dir([self.root / "one", self.root / "two"])

    def test_runtime_info_never_reads_secret_environment_values(self) -> None:
        with patch.dict(os.environ, {"BOT_TOKEN": "definitely-not-in-runtime-info"}):
            info = log_export.runtime_info(self.project)
        self.assertIn("Python version:", info)
        self.assertNotIn("definitely-not-in-runtime-info", info)
