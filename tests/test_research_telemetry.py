from __future__ import annotations

import csv
import gzip
import inspect
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

from oitgbot.models import MarkPriceUpdate, RollingOISample
from oitgbot.services import research_telemetry
from oitgbot.services.current_oi_collector import (
    CurrentOICollector,
    _CycleStats,
    _FetchedOI,
)
from oitgbot.services.price_state import PriceStateStore
from oitgbot.services.research_telemetry import (
    ResearchBar,
    ResearchTelemetry,
    ResearchTelemetryStore,
    bucket_start_utc,
)
from oitgbot.services.rolling_oi_store import RollingOIStore
from tools.research_telemetry_export import EXPORT_COLUMNS, export_recent

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def oi_sample(symbol: str, value: float, seconds: int) -> RollingOISample:
    timestamp = NOW + timedelta(seconds=seconds)
    return RollingOISample(symbol, value, timestamp, timestamp)


def price_update(symbol: str, value: float, seconds: int) -> MarkPriceUpdate:
    timestamp = NOW + timedelta(seconds=seconds)
    return MarkPriceUpdate(symbol, value, timestamp, timestamp)


def bar(
    symbol: str,
    bucket: datetime,
    *,
    oi: float | None = 100.0,
    price: float | None = 10.0,
    closed: bool = True,
) -> ResearchBar:
    return ResearchBar(
        symbol=symbol,
        bucket_start_utc=bucket,
        oi_open=oi,
        oi_high=oi,
        oi_low=oi,
        oi_close=oi,
        oi_sample_count=int(oi is not None),
        first_oi_observed_at_utc=bucket if oi is not None else None,
        last_oi_observed_at_utc=bucket if oi is not None else None,
        price_open=price,
        price_high=price,
        price_low=price,
        price_close=price,
        price_sample_count=int(price is not None),
        first_price_event_at_utc=bucket if price is not None else None,
        last_price_event_at_utc=bucket if price is not None else None,
        is_closed=closed,
    )


class ResearchAggregationTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "research.sqlite3"
        self.telemetry = ResearchTelemetry(self.db_path, clock=lambda: NOW)
        self.telemetry.start()
        self.telemetry.set_eligible_symbols(("BTCUSDT", "ETHUSDT"))

    def tearDown(self) -> None:
        self.telemetry.stop_sync()
        self.temporary.cleanup()

    def close_first_bucket(self) -> None:
        self.telemetry.observe_price(price_update("BTCUSDT", 99.0, 300))

    def test_utc_five_minute_bucket_alignment(self) -> None:
        value = datetime(2026, 8, 18, 12, 7, 59, 999999, tzinfo=timezone.utc)
        self.assertEqual(
            datetime(2026, 8, 18, 12, 5, tzinfo=timezone.utc),
            bucket_start_utc(value),
        )
        with self.assertRaisesRegex(ValueError, "UTC"):
            bucket_start_utc(value.replace(tzinfo=None))

    def test_oi_and_one_second_price_ohlc_are_aggregated_independently(self) -> None:
        for value, seconds in ((100.0, 5), (110.0, 35), (95.0, 65), (105.0, 95)):
            self.telemetry.observe_oi(oi_sample("BTCUSDT", value, seconds))
        for value, seconds in ((10.0, 1), (15.0, 11), (8.0, 21), (12.0, 31)):
            self.telemetry.observe_price(price_update("BTCUSDT", value, seconds))
        self.close_first_bucket()
        self.telemetry.stop_sync()

        row = ResearchTelemetryStore(self.db_path).rows()[0]
        self.assertEqual(
            (100.0, 110.0, 95.0, 105.0, 4),
            tuple(
                row[name]
                for name in (
                    "oi_open",
                    "oi_high",
                    "oi_low",
                    "oi_close",
                    "oi_sample_count",
                )
            ),
        )
        self.assertEqual(
            (10.0, 15.0, 8.0, 12.0, 4),
            tuple(
                row[name]
                for name in (
                    "price_open",
                    "price_high",
                    "price_low",
                    "price_close",
                    "price_sample_count",
                )
            ),
        )
        self.assertEqual(1, row["is_closed"])

    def test_price_high_low_between_oi_observations_is_preserved(self) -> None:
        self.telemetry.observe_oi(oi_sample("BTCUSDT", 100.0, 0))
        self.telemetry.observe_price(price_update("BTCUSDT", 10.0, 0))
        self.telemetry.observe_price(price_update("BTCUSDT", 25.0, 15))
        self.telemetry.observe_price(price_update("BTCUSDT", 4.0, 20))
        self.telemetry.observe_oi(oi_sample("BTCUSDT", 101.0, 30))
        self.telemetry.observe_price(price_update("BTCUSDT", 11.0, 30))
        self.close_first_bucket()
        self.telemetry.stop_sync()

        row = ResearchTelemetryStore(self.db_path).rows()[0]
        self.assertEqual(25.0, row["price_high"])
        self.assertEqual(4.0, row["price_low"])
        self.assertEqual(2, row["oi_sample_count"])

    def test_oi_only_and_price_only_rows_are_nullable_and_safe(self) -> None:
        self.telemetry.observe_oi(oi_sample("BTCUSDT", 100.0, 1))
        self.telemetry.observe_price(price_update("ETHUSDT", 20.0, 2))
        self.close_first_bucket()
        self.telemetry.stop_sync()

        rows = {
            row["symbol"]: row for row in ResearchTelemetryStore(self.db_path).rows()
        }
        self.assertIsNone(rows["BTCUSDT"]["price_open"])
        self.assertEqual(0, rows["BTCUSDT"]["price_sample_count"])
        self.assertIsNone(rows["ETHUSDT"]["oi_open"])
        self.assertEqual(0, rows["ETHUSDT"]["oi_sample_count"])

    def test_shutdown_persists_in_progress_bucket_as_explicit_partial(self) -> None:
        self.telemetry.observe_oi(oi_sample("BTCUSDT", 100.0, 1))
        self.telemetry.stop_sync()
        rows = ResearchTelemetryStore(self.db_path).rows(closed_only=False)
        self.assertEqual(1, len(rows))
        self.assertEqual(0, rows[0]["is_closed"])


class ResearchStorageTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "research.sqlite3"
        self.store = ResearchTelemetryStore(self.db_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_schema_is_idempotent_and_restart_preserves_rows(self) -> None:
        self.store.initialize()
        self.store.initialize()
        self.store.write_bars((bar("BTCUSDT", NOW),))
        reopened = ResearchTelemetryStore(self.db_path)
        reopened.initialize()

        rows = reopened.rows()
        self.assertEqual(1, len(rows))
        with reopened.connect(read_only=True) as connection:
            version = connection.execute(
                "SELECT value FROM schema_metadata WHERE key='schema_version'"
            ).fetchone()[0]
        self.assertEqual("1", version)

    def test_upsert_merges_partial_data_without_duplicate_key(self) -> None:
        self.store.initialize()
        self.store.write_bars(
            (bar("BTCUSDT", NOW, oi=100.0, price=None, closed=False),)
        )
        self.store.write_bars((bar("BTCUSDT", NOW, oi=None, price=12.0),))

        with self.store.connect(read_only=True) as connection:
            rows = list(connection.execute("SELECT * FROM research_bars_5m"))
        self.assertEqual(1, len(rows))
        self.assertEqual(100.0, rows[0]["oi_close"])
        self.assertEqual(12.0, rows[0]["price_close"])
        self.assertEqual(1, rows[0]["is_closed"])

    def test_retention_prunes_only_expired_rows(self) -> None:
        self.store.initialize()
        self.store.write_bars(
            (
                bar("OLDUSDT", NOW - timedelta(days=15)),
                bar("KEEPUSDT", NOW - timedelta(days=13)),
            )
        )
        self.assertEqual(1, self.store.prune(NOW - timedelta(days=14)))
        self.assertEqual(["KEEPUSDT"], [row["symbol"] for row in self.store.rows()])

    def test_export_filters_time_and_uses_deterministic_columns(self) -> None:
        self.store.initialize()
        self.store.write_bars(
            (
                bar("OLDUSDT", NOW - timedelta(hours=100)),
                bar("BTCUSDT", NOW - timedelta(hours=2)),
                bar("PARTIALUSDT", NOW - timedelta(hours=1), closed=False),
            )
        )
        output = Path(self.temporary.name) / "export.csv.gz"
        count, first, last = export_recent(self.db_path, output, hours=96, now_utc=NOW)
        self.assertEqual(1, count)
        self.assertEqual(first, last)
        with gzip.open(output, "rt", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(list(EXPORT_COLUMNS), rows[0])
        self.assertEqual("BTCUSDT", rows[1][0])

    def test_read_only_connection_cannot_write(self) -> None:
        self.store.initialize()
        with (
            self.store.connect(read_only=True) as connection,
            self.assertRaises(sqlite3.OperationalError),
        ):
            connection.execute("DELETE FROM research_bars_5m")

    def test_background_write_failure_is_contained(self) -> None:
        telemetry = ResearchTelemetry(self.db_path)
        telemetry.start()
        telemetry.set_eligible_symbols(("BTCUSDT",))

        def fail(_bars: object) -> int:
            raise sqlite3.OperationalError("synthetic write failure")

        telemetry.store.write_bars = fail  # type: ignore[method-assign]
        telemetry.observe_oi(oi_sample("BTCUSDT", 100.0, 1))
        telemetry.observe_oi(oi_sample("BTCUSDT", 101.0, 300))
        telemetry.stop_sync()
        self.assertGreaterEqual(telemetry.write_failures, 1)


class OICollectorTelemetryHookTests(TestCase):
    def test_duplicate_observation_is_not_sent_to_research_sink(self) -> None:
        accepted: list[RollingOISample] = []
        collector = object.__new__(CurrentOICollector)
        collector.future_oi_tolerance_seconds = 5.0
        collector.transaction_age_warning_seconds = 60.0
        collector.max_price_observation_skew_seconds = 5.0
        collector.rolling_store = RollingOIStore()
        collector.price_state = PriceStateStore()
        collector._observation_sink = accepted.append
        fetched_sample = oi_sample("BTCUSDT", 100.0, 1)
        fetched = _FetchedOI(
            type(
                "Reading",
                (),
                {
                    "symbol": fetched_sample.symbol,
                    "oi_quantity": fetched_sample.oi_quantity,
                    "exchange_time": fetched_sample.oi_exchange_time,
                },
            )(),
            fetched_sample.observed_at_utc,
        )

        collector._accept_fetched("BTCUSDT", fetched, _CycleStats())
        collector._accept_fetched("BTCUSDT", fetched, _CycleStats())
        older_sample = oi_sample("BTCUSDT", 90.0, 0)
        older = _FetchedOI(
            type(
                "Reading",
                (),
                {
                    "symbol": older_sample.symbol,
                    "oi_quantity": older_sample.oi_quantity,
                    "exchange_time": older_sample.oi_exchange_time,
                },
            )(),
            older_sample.observed_at_utc,
        )
        collector._accept_fetched("BTCUSDT", older, _CycleStats())

        self.assertEqual(1, len(accepted))


class ResearchArchitectureTests(TestCase):
    def test_research_layer_has_no_network_or_binance_client_dependency(self) -> None:
        source = inspect.getsource(research_telemetry)
        self.assertNotIn("requests", source)
        self.assertNotIn("websockets", source)
        self.assertNotIn("binance_api", source)
