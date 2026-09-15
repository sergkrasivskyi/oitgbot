from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

from oitgbot.app import build_shadow_runtime
from oitgbot.config import Settings
from oitgbot.models import MarkPriceUpdate, RollingOISample
from oitgbot.services.oi_flash import (
    OIFlashCandidate,
    OIFlashDetector,
    OIFlashDirection,
)
from oitgbot.services.oi_flash_publisher import OIFlashPublisher
from oitgbot.services.oi_flash_research import (
    FlashBar1m,
    OIFlashResearchStore,
    OIFlashResearchTelemetry,
    minute_start_ms,
    to_epoch_ms,
)
from oitgbot.services.oi_flash_runtime import OIFlashRuntime
from oitgbot.services.report_formatter import ReportFormatter
from oitgbot.services.rolling_oi_shadow_runtime import RollingOIShadowRuntime
from oitgbot.services.rolling_oi_store import RollingOIStore

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def add_sample(
    store: RollingOIStore,
    at: datetime,
    oi: float,
    *,
    symbol: str = "BTCUSDT",
    price: float | None = 100.0,
    price_at: datetime | None = None,
) -> None:
    store.add(
        RollingOISample(
            symbol,
            oi,
            at,
            at,
            mark_price=price,
            price_exchange_time=price_at if price_at is not None else at,
        )
    )


def candidate(
    at: datetime = NOW,
    *,
    symbol: str = "BTCUSDT",
    value: float = 3.5,
    price: float | None = 0.4,
) -> OIFlashCandidate:
    return OIFlashCandidate(
        detected_at_utc=at,
        symbol=symbol,
        direction=(
            OIFlashDirection.POSITIVE if value > 0 else OIFlashDirection.NEGATIVE
        ),
        oi_pct=value,
        px_pct=price,
        current_observed_at_utc=at,
        baseline_observed_at_utc=at - timedelta(seconds=60),
        actual_window_seconds=60.0,
        threshold_pct=3.0,
    )


class OIFlashDetectorTests(TestCase):
    def test_selects_latest_at_or_before_target_not_future_nearest(self) -> None:
        store = RollingOIStore()
        add_sample(store, NOW - timedelta(seconds=90), 90)
        add_sample(store, NOW - timedelta(seconds=75), 100, price=100)
        add_sample(store, NOW - timedelta(seconds=50), 200, price=200)
        add_sample(store, NOW, 104, price=101)

        event = OIFlashDetector().evaluate_symbol(store, "BTCUSDT", NOW)

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(NOW - timedelta(seconds=75), event.baseline_observed_at_utc)
        self.assertEqual(75, event.actual_window_seconds)
        self.assertAlmostEqual(4.0, event.oi_pct)
        self.assertAlmostEqual(1.0, event.px_pct or 0)

    def test_missing_or_out_of_tolerance_baseline_is_not_evaluable(self) -> None:
        for baseline in (None, NOW - timedelta(seconds=106)):
            with self.subTest(baseline=baseline):
                store = RollingOIStore()
                if baseline is not None:
                    add_sample(store, baseline, 100)
                add_sample(store, NOW, 104)
                self.assertIsNone(
                    OIFlashDetector().evaluate_symbol(store, "BTCUSDT", NOW)
                )

    def test_maximum_105_second_actual_window_is_evaluable(self) -> None:
        store = RollingOIStore()
        add_sample(store, NOW - timedelta(seconds=105), 100)
        add_sample(store, NOW, 104)
        event = OIFlashDetector().evaluate_symbol(store, "BTCUSDT", NOW)
        self.assertIsNotNone(event)
        self.assertEqual(105, event.actual_window_seconds)  # type: ignore[union-attr]

    def test_thresholds_are_inclusive(self) -> None:
        for value, expected in (
            (103.0, True),
            (97.0, True),
            (102.99, False),
            (97.01, False),
        ):
            with self.subTest(value=value):
                store = RollingOIStore()
                add_sample(store, NOW - timedelta(seconds=60), 100)
                add_sample(store, NOW, value)
                self.assertEqual(
                    expected,
                    OIFlashDetector().evaluate_symbol(store, "BTCUSDT", NOW)
                    is not None,
                )

    def test_price_availability_or_staleness_does_not_change_eligibility(self) -> None:
        for price, price_at in (
            (101.0, NOW),
            (None, None),
            (101.0, NOW - timedelta(seconds=6)),
            (101.0, NOW + timedelta(seconds=1)),
        ):
            with self.subTest(price=price, price_at=price_at):
                store = RollingOIStore()
                add_sample(store, NOW - timedelta(seconds=60), 100, price=100)
                add_sample(store, NOW, 104, price=price, price_at=price_at)
                event = OIFlashDetector().evaluate_symbol(store, "BTCUSDT", NOW)
                self.assertIsNotNone(event)
                self.assertAlmostEqual(4.0, event.oi_pct)  # type: ignore[union-attr]

    def test_only_requested_canonical_symbols_are_evaluated(self) -> None:
        store = RollingOIStore()
        for symbol in ("BTCUSDT", "FUTURESONLYUSDT"):
            add_sample(store, NOW - timedelta(seconds=60), 100, symbol=symbol)
            add_sample(store, NOW, 104, symbol=symbol)
        events = OIFlashDetector().evaluate(store, ("BTCUSDT",), NOW)
        self.assertEqual(["BTCUSDT"], [event.symbol for event in events])


class RecordingSender:
    def __init__(self, outcome: bool = True) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, str]] = []

    async def send_if_not_empty(self, chat_id, text, **_kwargs):
        self.calls.append((chat_id, text))
        return self.outcome


class OIFlashFormatterPublisherTests(IsolatedAsyncioTestCase):
    async def test_dedicated_target_format_order_links_and_spot_hints(self) -> None:
        sender = RecordingSender()
        formatter = ReportFormatter(
            spot_base_lookup=lambda symbol: {
                "1000PEPEUSDT": "PEPE",
                "BTCUSDT": "BTC",
            }.get(symbol)
        )
        events = (
            candidate(symbol="BTCUSDT", value=3.2, price=None),
            candidate(symbol="1000PEPEUSDT", value=-4.0, price=-0.18),
        )
        outcomes = await OIFlashPublisher(
            sender, formatter, chat_id="flash-chat"
        ).publish(events)

        self.assertEqual((True, True), outcomes)
        self.assertEqual("flash-chat", sender.calls[0][0])
        message = sender.calls[0][1]
        self.assertTrue(message.startswith("⚡ OI FLASH · 1m"))
        self.assertLess(message.index("OI -4.00%"), message.index("OI +3.20%"))
        self.assertIn("OI -4.00%", message)
        self.assertIn("OI +3.20%", message)
        self.assertNotIn("PX", message)
        self.assertIn("Binance_1000PEPEUSDT", message)
        self.assertIn("1000PEPEUSDT</a> · PEPE (S)", message)
        self.assertNotIn("BTCUSDT</a> · BTC (S)", message)


class OIFlashResearchStoreTests(TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "flash.sqlite3"
        self.store = OIFlashResearchStore(self.path)
        self.store.initialize()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_schema_epoch_ms_consecutive_minutes_and_invalid_data(self) -> None:
        minute = minute_start_ms(NOW - timedelta(minutes=2))
        bars = (
            FlashBar1m(
                "BTCUSDT", minute, 100, minute + 30_000, 10, minute + 59_000, 2, 50
            ),
            FlashBar1m(
                "BTCUSDT",
                minute + 60_000,
                103,
                minute + 90_000,
                11,
                minute + 119_000,
                2,
                60,
            ),
            FlashBar1m(
                "BTCUSDT", minute + 120_000, None, None, 12, minute + 179_000, 0, 60
            ),
        )
        self.assertEqual(3, self.store.write_bars(bars))
        with self.store.connect(read_only=True) as connection:
            rows = connection.execute(
                "SELECT *, typeof(minute_start_utc) AS timestamp_type "
                "FROM flash_bars_1m ORDER BY minute_start_utc"
            ).fetchall()
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertIn("flash_bars_1m", tables)
        self.assertIn("flash_events", tables)
        self.assertEqual("integer", rows[0]["timestamp_type"])
        self.assertIsNone(rows[0]["oi_1m_pct"])
        self.assertAlmostEqual(3.0, rows[1]["oi_1m_pct"])
        self.assertAlmostEqual(10.0, rows[1]["px_1m_pct"])
        self.assertEqual(0, rows[2]["valid_oi"])
        self.assertIsNone(rows[2]["oi_1m_pct"])
        self.assertEqual(1, rows[2]["valid_price"])

    def test_closed_minute_telemetry_uses_existing_observers(self) -> None:
        telemetry = OIFlashResearchTelemetry(self.path, store=self.store)
        telemetry.start()
        telemetry.set_eligible_symbols(("BTCUSDT",))
        first = NOW - timedelta(minutes=2)
        telemetry.observe_oi(RollingOISample("BTCUSDT", 100, first, first))
        telemetry.observe_price(MarkPriceUpdate("BTCUSDT", 10, first, first))
        next_minute = first + timedelta(minutes=1)
        telemetry.observe_oi(RollingOISample("BTCUSDT", 101, next_minute, next_minute))
        telemetry.observe_price(MarkPriceUpdate("BTCUSDT", 11, next_minute, next_minute))
        third_minute = next_minute + timedelta(minutes=1)
        telemetry.observe_oi(RollingOISample("BTCUSDT", 102, third_minute, third_minute))
        telemetry.stop_sync()
        with self.store.connect(read_only=True) as connection:
            rows = connection.execute(
                "SELECT * FROM flash_bars_1m ORDER BY minute_start_utc"
            ).fetchall()
        self.assertEqual(100, rows[0]["oi_close"])
        self.assertEqual(10, rows[0]["price_close"])
        self.assertEqual(11, rows[1]["price_close"])
        self.assertAlmostEqual(10.0, rows[1]["px_1m_pct"])
        self.assertEqual(1, rows[1]["valid_price"])
        self.assertEqual(1, rows[1]["price_observation_count"])

    def test_bar_retention_does_not_delete_events(self) -> None:
        old = NOW - timedelta(days=8)
        self.store.write_bars(
            (
                FlashBar1m(
                    "BTCUSDT",
                    minute_start_ms(old),
                    100,
                    to_epoch_ms(old),
                    None,
                    None,
                    1,
                    0,
                ),
            )
        )
        self.store.record_crossings(((candidate(old), False),))
        self.assertEqual(1, self.store.prune_bars(NOW - timedelta(days=7)))
        with self.store.connect(read_only=True) as connection:
            self.assertEqual(
                0,
                connection.execute("SELECT COUNT(*) FROM flash_bars_1m").fetchone()[0],
            )
            self.assertEqual(
                1, connection.execute("SELECT COUNT(*) FROM flash_events").fetchone()[0]
            )


class NoopTelemetry:
    retention_days = 7.0

    def start(self) -> None:
        return None

    def set_eligible_symbols(self, _symbols) -> None:
        return None

    def observe_oi(self, _sample) -> None:
        return None

    def observe_price(self, _update) -> None:
        return None

    async def stop(self) -> None:
        return None


class RecordingPublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[OIFlashCandidate, ...]] = []

    async def publish(self, events):
        self.calls.append(tuple(events))
        return tuple(True for _ in events)


class ControlledCooldownStore(OIFlashResearchStore):
    def __init__(self, path) -> None:
        super().__init__(path)
        self.fail_load = True
        self.load_calls = 0
        self.record_calls = 0

    def load_last_accepted(self):
        self.load_calls += 1
        if self.fail_load:
            raise sqlite3.OperationalError("database unavailable")
        return super().load_last_accepted()

    def record_crossings(self, decisions):
        self.record_calls += 1
        return super().record_crossings(decisions)


class OIFlashRuntimeTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "flash.sqlite3"
        self.store = OIFlashResearchStore(self.path)
        self.publisher = RecordingPublisher()
        self.runtime = OIFlashRuntime(
            db_path=str(self.path),
            store=self.store,
            telemetry=NoopTelemetry(),  # type: ignore[arg-type]
            publisher=self.publisher,
        )
        await self.runtime.start()

    async def asyncTearDown(self) -> None:
        await self.runtime.stop()
        self.tempdir.cleanup()

    async def test_directional_cooldown_boundaries_and_restart(self) -> None:
        events = (
            candidate(NOW, value=3.4),
            candidate(NOW + timedelta(minutes=4), value=4.8),
            candidate(NOW + timedelta(minutes=8), value=-3.2),
            candidate(NOW + timedelta(minutes=12), value=5.1),
            candidate(NOW + timedelta(minutes=15), value=3.3),
        )
        for event in events:
            await self.runtime._process_candidates((event,))
        self.assertEqual(3, len(self.publisher.calls))
        with self.store.connect(read_only=True) as connection:
            rows = connection.execute(
                "SELECT suppressed_by_cooldown FROM flash_events ORDER BY id"
            ).fetchall()
        self.assertEqual([0, 1, 0, 1, 0], [row[0] for row in rows])

        restarted_publisher = RecordingPublisher()
        restarted = OIFlashRuntime(
            db_path=str(self.path),
            store=self.store,
            telemetry=NoopTelemetry(),  # type: ignore[arg-type]
            publisher=restarted_publisher,
        )
        await restarted.start()
        await restarted._process_candidates(
            (candidate(NOW + timedelta(minutes=15, seconds=1), value=3.6),)
        )
        await restarted.stop()
        self.assertEqual([], restarted_publisher.calls)

    def _runtime_with_store(self, store, publisher):
        return OIFlashRuntime(
            db_path=str(store.path),
            store=store,
            telemetry=NoopTelemetry(),  # type: ignore[arg-type]
            publisher=publisher,
        )

    async def test_failed_startup_restore_recovers_and_suppresses_same_direction(
        self,
    ) -> None:
        self.store.record_crossings(((candidate(NOW, value=3.4), False),))
        store = ControlledCooldownStore(self.path)
        publisher = RecordingPublisher()
        runtime = self._runtime_with_store(store, publisher)

        with self.assertLogs("oitgbot.rolling.oi_flash", level="WARNING"):
            await runtime.start()
        store.fail_load = False
        await runtime._process_candidates(
            (candidate(NOW + timedelta(minutes=7), value=3.6),)
        )
        await runtime.stop()

        self.assertEqual(2, store.load_calls)
        self.assertEqual([], publisher.calls)
        with self.store.connect(read_only=True) as connection:
            rows = connection.execute(
                "SELECT suppressed_by_cooldown FROM flash_events ORDER BY id"
            ).fetchall()
        self.assertEqual([0, 1], [row[0] for row in rows])

    async def test_failed_startup_restore_recovers_empty_and_accepts(self) -> None:
        path = Path(self.tempdir.name) / "empty-recovery.sqlite3"
        store = ControlledCooldownStore(path)
        publisher = RecordingPublisher()
        runtime = self._runtime_with_store(store, publisher)

        with self.assertLogs("oitgbot.rolling.oi_flash", level="WARNING"):
            await runtime.start()
        store.fail_load = False
        await runtime._process_candidates((candidate(),))
        await runtime._process_candidates(
            (candidate(NOW + timedelta(minutes=16), value=3.5),)
        )
        await runtime.stop()

        self.assertEqual(2, store.load_calls)
        self.assertEqual(2, store.record_calls)
        self.assertEqual(2, len(publisher.calls))

    async def test_unknown_cooldown_blocks_publication_when_reload_still_fails(
        self,
    ) -> None:
        store = ControlledCooldownStore(
            Path(self.tempdir.name) / "unavailable.sqlite3"
        )
        publisher = RecordingPublisher()
        runtime = self._runtime_with_store(store, publisher)

        with self.assertLogs("oitgbot.rolling.oi_flash", level="WARNING"):
            await runtime.start()
        with self.assertLogs("oitgbot.rolling.oi_flash", level="WARNING") as logs:
            await runtime._process_candidates((candidate(),))
        await runtime.stop()

        self.assertEqual(2, store.load_calls)
        self.assertEqual(0, store.record_calls)
        self.assertEqual([], publisher.calls)
        self.assertTrue(any("cooldown_state_unknown" in line for line in logs.output))

    async def test_recovered_restore_keeps_opposite_direction_independent(self) -> None:
        self.store.record_crossings(((candidate(NOW, value=3.4), False),))
        store = ControlledCooldownStore(self.path)
        publisher = RecordingPublisher()
        runtime = self._runtime_with_store(store, publisher)

        with self.assertLogs("oitgbot.rolling.oi_flash", level="WARNING"):
            await runtime.start()
        store.fail_load = False
        await runtime._process_candidates(
            (candidate(NOW + timedelta(minutes=7), value=-3.2),)
        )
        await runtime.stop()

        self.assertEqual(2, store.load_calls)
        self.assertEqual(1, len(publisher.calls))
        self.assertEqual("negative", publisher.calls[0][0].direction.value)

    async def test_qualifying_oi_without_price_is_accepted_and_published(self) -> None:
        rolling_store = RollingOIStore()
        add_sample(
            rolling_store,
            NOW - timedelta(seconds=60),
            100,
            price=None,
        )
        add_sample(rolling_store, NOW, 104, price=None)
        events = self.runtime.detector.evaluate(rolling_store, ("BTCUSDT",), NOW)

        self.assertEqual(1, len(events))
        self.assertIsNone(events[0].px_pct)
        await self.runtime._process_candidates(events)

        self.assertEqual(1, len(self.publisher.calls))
        with self.store.connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT px_pct, suppressed_by_cooldown, telegram_sent "
                "FROM flash_events"
            ).fetchone()
        self.assertEqual((None, 0, 1), tuple(row))

    async def test_suppressed_crossing_and_publish_state_are_persisted(self) -> None:
        await self.runtime._process_candidates((candidate(),))
        await self.runtime._process_candidates(
            (candidate(NOW + timedelta(seconds=30), value=4.0),)
        )
        with self.store.connect(read_only=True) as connection:
            rows = connection.execute(
                "SELECT suppressed_by_cooldown, telegram_attempted, telegram_sent "
                "FROM flash_events ORDER BY id"
            ).fetchall()
        self.assertEqual((0, 1, 1), tuple(rows[0]))
        self.assertEqual((1, 0, 0), tuple(rows[1]))

    async def test_db_write_failure_blocks_telegram_without_escaping(self) -> None:
        class FailingStore(OIFlashResearchStore):
            def record_crossings(self, _decisions):
                raise sqlite3.OperationalError("disk unavailable")

        publisher = RecordingPublisher()
        runtime = OIFlashRuntime(
            db_path=str(self.path),
            store=FailingStore(self.path),
            telemetry=NoopTelemetry(),  # type: ignore[arg-type]
            publisher=publisher,
        )
        with self.assertLogs("oitgbot.rolling.oi_flash", level="ERROR"):
            await runtime._process_candidates((candidate(),))
        self.assertEqual([], publisher.calls)

    async def test_event_exists_before_publish(self) -> None:
        store = self.store

        class InspectingPublisher:
            async def publish(self, events):
                with store.connect(read_only=True) as connection:
                    row = connection.execute(
                        "SELECT telegram_attempted FROM flash_events"
                    ).fetchone()
                assert row[0] == 1
                return (True,)

        self.runtime.publisher = InspectingPublisher()
        await self.runtime._process_candidates((candidate(),))


class OIFlashCycleGateTests(TestCase):
    def test_only_fully_successful_cycle_reaches_flash(self) -> None:
        class Flash:
            def __init__(self) -> None:
                self.calls = 0

            def submit_cycle(self, *_args) -> None:
                self.calls += 1

        runtime = object.__new__(RollingOIShadowRuntime)
        runtime.flash_runtime = Flash()
        runtime.rolling_store = RollingOIStore()
        good = SimpleNamespace(
            cycle_skipped=False,
            skip_reason=None,
            cycle_timed_out=False,
            timed_out_symbols=0,
            failed_symbols=0,
            symbols_requested=1,
            successful_samples=1,
            cycle_finished_at_utc=NOW,
        )
        runtime._submit_flash_cycle(good, ("BTCUSDT",))
        runtime._submit_flash_cycle(
            SimpleNamespace(**{**vars(good), "failed_symbols": 1}),
            ("BTCUSDT",),
        )
        self.assertEqual(1, runtime.flash_runtime.calls)


class OIFlashConfigurationTests(TestCase):
    def test_failed_flash_observer_does_not_block_existing_observer(self) -> None:
        observed = []

        def failed(_value):
            raise OSError("flash unavailable")

        notify = RollingOIShadowRuntime._composite_observer(
            "oi", (failed, observed.append)
        )
        with self.assertLogs("oitgbot.rolling.runtime", level="ERROR"):
            notify("sample")
        self.assertEqual(["sample"], observed)

    def test_safe_defaults_and_validation(self) -> None:
        configured = Settings(telegram_publish_enabled=False)
        self.assertFalse(configured.oi_flash_enabled)
        self.assertFalse(configured.oi_flash_telegram_enabled)
        self.assertEqual(
            "state/oi_flash_research.sqlite3", configured.oi_flash_research_db_path
        )
        configured.validate()

    def test_flash_telegram_requires_enabled_global_publish_and_chat(self) -> None:
        configured = Settings(
            telegram_publish_enabled=False,
            oi_flash_enabled=True,
            oi_flash_telegram_enabled=True,
        )
        with self.assertRaisesRegex(RuntimeError, "OI_FLASH"):
            configured.validate()

    def test_build_reuses_shadow_runtime_instead_of_new_market_data(self) -> None:
        configured = replace(
            Settings(telegram_publish_enabled=False), oi_flash_enabled=True
        )
        with (
            patch("oitgbot.app.settings", configured),
            patch("oitgbot.app.RollingOIShadowRuntime") as runtime_type,
        ):
            build_shadow_runtime(
                object(), SimpleNamespace(get_symbols_cached=lambda: ())
            )
        flash = runtime_type.call_args.kwargs["flash_runtime"]
        self.assertIsInstance(flash, OIFlashRuntime)
        self.assertFalse(hasattr(flash, "binance_api"))
