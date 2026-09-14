from __future__ import annotations

import csv
import gzip
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

from oitgbot.services.oi_flash import OIFlashCandidate, OIFlashDirection
from oitgbot.services.oi_flash_research import (
    FlashBar1m,
    OIFlashResearchStore,
    minute_start_ms,
    to_epoch_ms,
)
from tools.flash_research_export import export_flash_research
from tools.flash_research_status import collect_status

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


class FlashResearchToolsTests(TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db = self.root / "flash.sqlite3"
        self.store = OIFlashResearchStore(self.db)
        minute = NOW - timedelta(minutes=1)
        self.store.write_bars(
            (
                FlashBar1m(
                    "BTCUSDT",
                    minute_start_ms(minute),
                    103,
                    to_epoch_ms(minute),
                    10,
                    to_epoch_ms(minute),
                    2,
                    50,
                ),
            )
        )
        event = OIFlashCandidate(
            NOW,
            "BTCUSDT",
            OIFlashDirection.POSITIVE,
            3.0,
            None,
            NOW,
            NOW - timedelta(seconds=60),
            60,
            3.0,
        )
        self.store.record_crossings(((event, False),))

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_status_reports_health_coverage_and_event_counts(self) -> None:
        status = collect_status(self.db, now_utc=NOW)
        self.assertEqual("ok", status["integrity"])
        self.assertEqual(1, status["bars"])
        self.assertEqual(1, status["symbols"])
        self.assertEqual(1, status["events"])
        self.assertEqual(1, status["accepted"])
        self.assertEqual(0, status["cooldown_suppressed"])
        self.assertTrue(status["oldest_bar_utc"].endswith("+00:00"))

    def test_export_writes_separate_human_readable_gzip_files(self) -> None:
        bars_path, bars, events_path, events = export_flash_research(
            self.db,
            self.root / "flash-research-7d.csv.gz",
            now_utc=NOW,
        )
        self.assertEqual((1, 1), (bars, events))
        self.assertEqual("flash-research-7d-bars.csv.gz", bars_path.name)
        self.assertEqual("flash-research-7d-events.csv.gz", events_path.name)
        with gzip.open(bars_path, "rt", encoding="utf-8", newline="") as handle:
            row = next(csv.DictReader(handle))
        self.assertTrue(row["minute_start_utc"].endswith("+00:00"))
