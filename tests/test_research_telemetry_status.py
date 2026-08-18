from __future__ import annotations

import hashlib
import io
import tempfile
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from oitgbot.services.research_telemetry import ResearchBar, ResearchTelemetryStore
from tools.research_telemetry_export import export_recent
from tools.research_telemetry_export import main as export_main
from tools.research_telemetry_status import format_status, inspect_telemetry, main

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def research_bar(
    symbol: str,
    bucket: datetime,
    *,
    oi_samples: int,
    price_samples: int,
    closed: bool = True,
) -> ResearchBar:
    return ResearchBar(
        symbol=symbol,
        bucket_start_utc=bucket,
        oi_open=100.0 if oi_samples else None,
        oi_high=101.0 if oi_samples else None,
        oi_low=99.0 if oi_samples else None,
        oi_close=100.5 if oi_samples else None,
        oi_sample_count=oi_samples,
        first_oi_observed_at_utc=bucket if oi_samples else None,
        last_oi_observed_at_utc=bucket if oi_samples else None,
        price_open=10.0 if price_samples else None,
        price_high=11.0 if price_samples else None,
        price_low=9.0 if price_samples else None,
        price_close=10.5 if price_samples else None,
        price_sample_count=price_samples,
        first_price_event_at_utc=bucket if price_samples else None,
        last_price_event_at_utc=bucket if price_samples else None,
        is_closed=closed,
    )


class ResearchTelemetryStatusTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "status.sqlite3"
        self.store = ResearchTelemetryStore(self.db_path)
        self.store.initialize()
        first = NOW
        latest = NOW + timedelta(minutes=5)
        self.store.write_bars(
            (
                research_bar("BTCUSDT", first, oi_samples=2, price_samples=10),
                research_bar("ETHUSDT", first, oi_samples=3, price_samples=0),
                research_bar("BTCUSDT", latest, oi_samples=4, price_samples=8),
                research_bar("ETHUSDT", latest, oi_samples=0, price_samples=6),
                research_bar(
                    "SOLUSDT", latest, oi_samples=1, price_samples=1, closed=False
                ),
            )
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_status_reports_closed_partial_quality_and_latest_bucket_metrics(
        self,
    ) -> None:
        status = inspect_telemetry(self.db_path)

        self.assertEqual(4, status.closed_bars)
        self.assertEqual(2, status.closed_symbols)
        self.assertEqual(NOW.isoformat(), status.first_closed_bucket_utc)
        self.assertEqual(
            (NOW + timedelta(minutes=5)).isoformat(), status.last_closed_bucket_utc
        )
        self.assertEqual(1, status.partial_bars)
        self.assertEqual(3, status.closed_bars_with_oi)
        self.assertEqual(
            (2, 3, 4),
            (
                status.oi_samples.minimum,
                status.oi_samples.median,
                status.oi_samples.maximum,
            ),
        )
        self.assertEqual(1, status.oi_without_price)
        self.assertEqual(3, status.closed_bars_with_price)
        self.assertEqual(
            (6, 8, 10),
            (
                status.price_samples.minimum,
                status.price_samples.median,
                status.price_samples.maximum,
            ),
        )
        self.assertEqual(1, status.price_without_oi)
        self.assertEqual(0, status.duplicate_symbol_bucket_groups)
        self.assertEqual(0, status.invalid_bucket_count)
        self.assertEqual(0, status.null_or_empty_symbol_count)

        latest = status.latest_bucket
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(2, latest.total_bars)
        self.assertEqual(
            (1, 2, 1),
            (latest.bars_with_oi, latest.bars_with_price, latest.bars_with_both),
        )
        self.assertEqual(
            (4, 4, 4),
            (
                latest.oi_samples.minimum,
                latest.oi_samples.median,
                latest.oi_samples.maximum,
            ),
        )
        self.assertEqual(
            (6, 7, 8),
            (
                latest.price_samples.minimum,
                latest.price_samples.median,
                latest.price_samples.maximum,
            ),
        )

    def test_status_detects_non_aligned_bucket_and_never_mutates_database(self) -> None:
        bad = replace(
            research_bar("BADUSDT", NOW, oi_samples=1, price_samples=1),
            bucket_start_utc=NOW + timedelta(seconds=1),
        )
        self.store.write_bars((bad,))
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()

        status = inspect_telemetry(self.db_path)

        after = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual(1, status.invalid_bucket_count)

    def test_status_cli_is_concise_and_read_only(self) -> None:
        output = io.StringIO()
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        with redirect_stdout(output):
            self.assertEqual(0, main(["--db", str(self.db_path)]))
        after = hashlib.sha256(self.db_path.read_bytes()).hexdigest()

        text = output.getvalue()
        self.assertEqual(before, after)
        self.assertIn("Closed 5m bars: 4", text)
        self.assertIn("Latest closed bucket", text)
        self.assertIn("Duplicate symbol+bucket groups: 0", text)
        self.assertLess(len(text.splitlines()), 40)
        self.assertIn("DB path:", format_status(inspect_telemetry(self.db_path)))

    def test_export_explicit_db_reads_requested_database(self) -> None:
        other_db = Path(self.temporary.name) / "other.sqlite3"
        other_store = ResearchTelemetryStore(other_db)
        other_store.initialize()
        other_store.write_bars(
            (research_bar("ONLYOTHERUSDT", NOW, oi_samples=1, price_samples=1),)
        )
        output = Path(self.temporary.name) / "other.csv.gz"

        count, first, last = export_recent(other_db, output, hours=24, now_utc=NOW)

        self.assertEqual(1, count)
        self.assertEqual(NOW.isoformat(), first)
        self.assertEqual(NOW.isoformat(), last)

    def test_export_default_db_argument_is_preserved(self) -> None:
        output = Path(self.temporary.name) / "default.csv.gz"
        with (
            patch(
                "tools.research_telemetry_export.export_recent",
                return_value=(0, None, None),
            ) as export,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(0, export_main(["--hours", "1", "--output", str(output)]))

        self.assertEqual(Path("state/oi_research.sqlite3"), export.call_args.args[0])
