from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

from oitgbot.app import build_shadow_runtime
from oitgbot.clients.telegram_sender import TelegramSender
from oitgbot.config import Settings
from oitgbot.models import (
    BinanceRateLimit,
    CurrentOpenInterest,
    RollingOISample,
)
from oitgbot.services.current_oi_collector import (
    CurrentOICollector,
    CurrentOICycleResult,
)
from oitgbot.services.mark_price_stream import PriceStreamHealth
from oitgbot.services.rate_limit_budget import RateLimitBudget
from oitgbot.services.rolling_oi_shadow_runtime import RollingOIShadowRuntime


NOW = datetime(2025, 1, 1, 2, 0, tzinfo=timezone.utc)


def cycle_result(
    *,
    inserted: int = 1,
    requested: int = 1,
    successful: int = 1,
    failed: int = 0,
    http_429: int = 0,
    http_418: int = 0,
) -> CurrentOICycleResult:
    return CurrentOICycleResult(
        cycle_started_at_utc=NOW,
        cycle_finished_at_utc=NOW,
        elapsed_seconds=0.1,
        symbols_requested=requested,
        oi_requests_attempted=requested,
        successful_samples=successful,
        failed_symbols=failed,
        future_oi_rejected=0,
        old_transaction_time_count=0,
        transaction_time_unchanged=0,
        price_fresh=successful,
        price_missing=0,
        price_receipt_stale=0,
        price_alignment_rejected=0,
        samples_inserted=inserted,
        samples_ignored_duplicate_or_out_of_order=0,
        timed_out_symbols=0,
        http_429_errors=http_429,
        http_418_errors=http_418,
        cycle_timed_out=False,
        cycle_skipped=False,
        skip_reason=None,
        rate_budget_state="SAFE",
        failure_counts=(),
        transaction_age_min_s=0.1,
        transaction_age_median_s=0.1,
        transaction_age_p95_s=0.2,
        transaction_age_max_s=0.2,
        price_receipt_age_max_s=0.1,
        price_event_age_abs_max_s=0.1,
        price_oi_transaction_skew_abs_max_s=0.1,
    )


class FakeAPI:
    def __init__(self, rate_limits: list[BinanceRateLimit] | None = None) -> None:
        self.rate_limits = rate_limits if rate_limits is not None else [
            BinanceRateLimit("REQUEST_WEIGHT", "MINUTE", 1, 2400)
        ]
        self.rate_calls = 0

    def get_request_weight_limits(self) -> list[BinanceRateLimit]:
        self.rate_calls += 1
        return self.rate_limits


class FakeStream:
    def __init__(self, _store: object, *, fail: bool = False) -> None:
        self.fail = fail
        self.run_calls = 0
        self.stop_calls = 0
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def run(self) -> None:
        self.run_calls += 1
        self.started.set()
        if self.fail:
            raise ConnectionError("price unavailable")
        await self.stopped.wait()

    async def stop(self) -> None:
        self.stop_calls += 1
        self.stopped.set()

    def health(self, _now: datetime, max_age_seconds: float) -> PriceStreamHealth:
        del max_age_seconds
        return PriceStreamHealth(
            connected=self.started.is_set() and not self.stopped.is_set(),
            last_message_received_at=None,
            last_valid_update_at=None,
            reconnect_count=0,
            stale=True,
            last_error="unavailable" if self.fail else None,
        )


class FakeCollector:
    def __init__(self, results: list[CurrentOICycleResult] | None = None) -> None:
        self.results = list(results or [cycle_result(inserted=0)])
        self.calls: list[tuple[str, ...]] = []
        self.close_calls = 0

    async def collect_cycle(self, symbols: object, **_kwargs: object) -> CurrentOICycleResult:
        self.calls.append(tuple(symbols))  # type: ignore[arg-type]
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]

    async def close(self) -> None:
        self.close_calls += 1


class ShadowConfigTests(TestCase):
    def test_defaults_are_safe_phase_one_values(self) -> None:
        value = Settings(bot_token="x", all_channel_id="a", prop_channel_id="p")

        self.assertTrue(value.rolling_oi_shadow_enabled)
        self.assertEqual(30, value.rolling_oi_cadence_seconds)
        self.assertEqual(20, value.rolling_oi_workers)
        self.assertEqual(150, value.rolling_oi_retention_minutes)
        self.assertEqual(5, value.rolling_oi_price_max_age_seconds)
        self.assertEqual(60, value.rolling_oi_observation_max_age_seconds)
        self.assertEqual(60, value.rolling_oi_transaction_age_warning_seconds)
        self.assertEqual(5, value.rolling_oi_5m_trigger_pct)
        self.assertEqual(3, value.rolling_oi_5m_rearm_pct)
        self.assertEqual(15, value.rolling_oi_signal_state_ttl_minutes)
        self.assertTrue(value.research_telemetry_enabled)
        self.assertEqual("state/oi_research.sqlite3", value.research_telemetry_db_path)
        self.assertEqual(14, value.research_telemetry_retention_days)

    def test_invalid_enabled_shadow_config_fails_clearly(self) -> None:
        value = Settings(
            bot_token="x",
            all_channel_id="a",
            prop_channel_id="p",
            rolling_oi_retention_minutes=119,
        )

        with self.assertRaisesRegex(RuntimeError, "RETENTION_MINUTES"):
            value.validate()

    def test_invalid_signal_hysteresis_fails_clearly(self) -> None:
        value = Settings(
            bot_token="x",
            all_channel_id="a",
            prop_channel_id="p",
            rolling_oi_5m_trigger_pct=5,
            rolling_oi_5m_rearm_pct=5,
        )

        with self.assertRaisesRegex(RuntimeError, "REARM_PCT"):
            value.validate()

    def test_disabled_shadow_does_not_construct_runtime(self) -> None:
        disabled = SimpleNamespace(rolling_oi_shadow_enabled=False)
        with (
            patch("oitgbot.app.settings", disabled),
            patch("oitgbot.app.RollingOIShadowRuntime") as runtime_type,
        ):
            result = build_shadow_runtime(object(), object())  # type: ignore[arg-type]

        self.assertIsNone(result)
        runtime_type.assert_not_called()

    def test_invalid_research_telemetry_config_fails_clearly(self) -> None:
        value = Settings(
            bot_token="x",
            all_channel_id="a",
            prop_channel_id="p",
            research_telemetry_retention_days=0,
        )
        with self.assertRaisesRegex(RuntimeError, "RESEARCH_TELEMETRY_RETENTION_DAYS"):
            value.validate()

    def test_app_wires_default_research_telemetry_settings(self) -> None:
        configured = Settings(
            bot_token="x", all_channel_id="a", prop_channel_id="p"
        )
        jobs = SimpleNamespace(get_symbols_cached=lambda: ["BTCUSDT"])
        with (
            patch("oitgbot.app.settings", configured),
            patch("oitgbot.app.RollingOIShadowRuntime") as runtime_type,
        ):
            build_shadow_runtime(object(), jobs)  # type: ignore[arg-type]

        kwargs = runtime_type.call_args.kwargs
        self.assertTrue(kwargs["research_telemetry_enabled"])
        self.assertEqual("state/oi_research.sqlite3", kwargs["research_db_path"])
        self.assertEqual(14, kwargs["research_retention_days"])


class ShadowRuntimeLifecycleTests(IsolatedAsyncioTestCase):
    async def test_partial_collector_failure_preserves_valid_quantity_sample(self) -> None:
        class PartialAPI(FakeAPI):
            def get_current_open_interest(self, symbol: str) -> CurrentOpenInterest:
                if symbol == "ETHUSDT":
                    raise ConnectionError("synthetic failure")
                return CurrentOpenInterest(symbol, 100.0, NOW)

        api = PartialAPI()
        runtime = RollingOIShadowRuntime(
            api,
            lambda: ["BTCUSDT", "ETHUSDT"],
            stream_factory=FakeStream,
        )
        runtime.collector = CurrentOICollector(
            api,
            runtime.price_state,
            runtime.rolling_store,
            RateLimitBudget(api.rate_limits),
            clock=lambda: NOW,
        )

        result = await runtime.run_cycle()

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(1, result.samples_inserted)
        self.assertEqual(1, result.failed_symbols)
        self.assertIsNotNone(runtime.rolling_store.latest("BTCUSDT"))
        self.assertIsNone(runtime.rolling_store.latest("ETHUSDT"))
        self.assertEqual("partial_failure", runtime.health().collector_last_cycle_state)
        await runtime.stop()

    async def test_repeated_synthetic_collection_builds_a_5m_window_without_sleep(self) -> None:
        class CyclingAPI(FakeAPI):
            def __init__(self) -> None:
                super().__init__()
                self.now = NOW - timedelta(minutes=5)
                self.quantity = 100.0

            def get_current_open_interest(self, symbol: str) -> CurrentOpenInterest:
                return CurrentOpenInterest(symbol, self.quantity, self.now)

        api = CyclingAPI()
        runtime = RollingOIShadowRuntime(
            api,
            lambda: ["BTCUSDT"],
            stream_factory=FakeStream,
        )
        runtime.collector = CurrentOICollector(
            api,
            runtime.price_state,
            runtime.rolling_store,
            RateLimitBudget(api.rate_limits),
            clock=lambda: api.now,
        )

        first = await runtime.run_cycle()
        self.assertIsNotNone(first)
        self.assertFalse(
            runtime.calculator.calculate_5m(
                runtime.rolling_store, "BTCUSDT"
            ).available
        )
        for _ in range(10):
            api.now += timedelta(seconds=30)
            api.quantity += 1
            await runtime.run_cycle()

        self.assertEqual(11, len(runtime.rolling_store.history("BTCUSDT")))
        self.assertTrue(
            runtime.calculator.calculate_5m(
                runtime.rolling_store, "BTCUSDT"
            ).available
        )
        await runtime.stop()

    async def test_start_constructs_and_starts_services_once(self) -> None:
        api = FakeAPI()
        stream = FakeStream(None)
        collector = FakeCollector()
        collector_creations = 0

        def make_collector(*_args: object, **_kwargs: object) -> FakeCollector:
            nonlocal collector_creations
            collector_creations += 1
            return collector

        runtime = RollingOIShadowRuntime(
            api,
            lambda: ["BTCUSDT"],
            stream_factory=lambda _store: stream,
            collector_factory=make_collector,
        )
        await runtime.start()
        await runtime.start()
        await runtime.wait_initialized()
        await stream.started.wait()

        self.assertEqual(1, stream.run_calls)
        self.assertEqual(1, collector_creations)
        self.assertEqual(1, api.rate_calls)
        self.assertEqual("SAFE", runtime.health().rate_budget_state)
        await runtime.stop()

    async def test_missing_rate_limits_disables_polling_but_keeps_stream_owned(self) -> None:
        stream = FakeStream(None)
        collector_factory_calls = 0

        def collector_factory(*_args: object, **_kwargs: object) -> FakeCollector:
            nonlocal collector_factory_calls
            collector_factory_calls += 1
            return FakeCollector()

        runtime = RollingOIShadowRuntime(
            FakeAPI([]),
            lambda: ["BTCUSDT"],
            stream_factory=lambda _store: stream,
            collector_factory=collector_factory,
        )
        await runtime.start()
        await runtime.wait_initialized()

        self.assertEqual(0, collector_factory_calls)
        self.assertEqual("UNSAFE", runtime.health().rate_budget_state)
        self.assertEqual("rate_budget_unavailable", runtime.health().collector_last_cycle_state)
        await runtime.stop()

    async def test_price_stream_failure_does_not_prevent_quantity_collector_start(self) -> None:
        stream = FakeStream(None, fail=True)
        collector = FakeCollector()
        runtime = RollingOIShadowRuntime(
            FakeAPI(),
            lambda: ["BTCUSDT"],
            stream_factory=lambda _store: stream,
            collector_factory=lambda *_args, **_kwargs: collector,
        )
        await runtime.start()
        await runtime.wait_initialized()
        async def wait_for_collector() -> None:
            while not collector.calls:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_collector(), timeout=1)

        self.assertIs(runtime.collector, collector)
        self.assertTrue(collector.calls)
        await runtime.stop()

    async def test_research_start_failure_does_not_prevent_production_runtime(self) -> None:
        class FailingResearch:
            def start(self) -> None:
                raise OSError("synthetic database failure")

            def set_eligible_symbols(self, _symbols: object) -> None:
                return None

            def observe_oi(self, _sample: object) -> None:
                return None

            def observe_price(self, _update: object) -> None:
                return None

            async def stop(self) -> None:
                return None

        collector = FakeCollector()
        runtime = RollingOIShadowRuntime(
            FakeAPI(),
            lambda: ["BTCUSDT"],
            stream_factory=FakeStream,
            collector_factory=lambda *_args, **_kwargs: collector,
            research_telemetry_enabled=True,
            research_factory=lambda *_args, **_kwargs: FailingResearch(),
        )
        with self.assertLogs("oitgbot.rolling.runtime", level="ERROR") as captured:
            await runtime.start()
        await runtime.wait_initialized()

        async def wait_for_collector() -> None:
            while not collector.calls:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_collector(), timeout=1)
        self.assertIn("reason=start_failed", "\n".join(captured.output))
        self.assertTrue(collector.calls)
        await runtime.stop()

    async def test_http_429_backoff_and_418_protection_are_distinct(self) -> None:
        runtime = RollingOIShadowRuntime(
            FakeAPI(),
            lambda: ["BTCUSDT"],
            stream_factory=FakeStream,
        )
        collector = FakeCollector(
            [
                cycle_result(inserted=0, failed=1, successful=0, http_429=1),
                cycle_result(inserted=0, failed=1, successful=0, http_418=1),
            ]
        )
        runtime.collector = collector

        await runtime.run_cycle()
        self.assertEqual("rate_pressure_http_429", runtime.health().collector_last_cycle_state)
        self.assertGreater(runtime._backoff_until, asyncio.get_running_loop().time())

        await runtime.run_cycle()
        self.assertEqual("protected_http_418", runtime.health().collector_last_cycle_state)
        self.assertIsNone(await runtime.run_cycle())
        self.assertEqual(2, len(collector.calls))
        await runtime.stop()

    async def test_shutdown_stops_periodic_collector_stream_and_executor_owner(self) -> None:
        stream = FakeStream(None)
        collector = FakeCollector()
        runtime = RollingOIShadowRuntime(
            FakeAPI(),
            lambda: ["BTCUSDT"],
            cadence_seconds=3600,
            stream_factory=lambda _store: stream,
            collector_factory=lambda *_args, **_kwargs: collector,
        )
        await runtime.start()
        await runtime.wait_initialized()
        await stream.started.wait()
        await runtime.stop()
        await runtime.stop()

        self.assertEqual(1, collector.close_calls)
        self.assertEqual(1, stream.stop_calls)
        self.assertTrue(runtime._periodic_task is None or runtime._periodic_task.done())
        self.assertTrue(runtime._stream_task is None or runtime._stream_task.done())


class ShadowRuntimeEvaluationTests(TestCase):
    def make_runtime(self) -> RollingOIShadowRuntime:
        return RollingOIShadowRuntime(
            FakeAPI(),
            lambda: ["BTCUSDT"],
            stream_factory=FakeStream,
            clock=lambda: NOW,
        )

    @staticmethod
    def add_history(runtime: RollingOIShadowRuntime, quantity_at: object) -> None:
        start = NOW - timedelta(minutes=120)
        for minute in range(0, 121, 5):
            timestamp = start + timedelta(minutes=minute)
            quantity = quantity_at(minute)  # type: ignore[operator]
            runtime.rolling_store.add(
                RollingOISample(
                    symbol="BTCUSDT",
                    oi_quantity=quantity,
                    observed_at_utc=timestamp,
                    oi_exchange_time=timestamp,
                    mark_price=100 + minute,
                    price_exchange_time=timestamp,
                )
            )

    def test_warmup_readiness_uses_actual_history_not_uptime(self) -> None:
        runtime = self.make_runtime()
        runtime.rolling_store.add(
            RollingOISample(
                "BTCUSDT",
                100,
                NOW - timedelta(minutes=120),
                NOW - timedelta(minutes=120),
            )
        )

        cold = runtime.evaluate_and_log(cycle_result())
        self.assertEqual((0, 0, 0, 0), (
            cold.ready_5m, cold.ready_20m, cold.ready_60m, cold.ready_120m
        ))

        self.add_history(runtime, lambda minute: 100 + minute)
        warm = runtime.evaluate_and_log(cycle_result())
        self.assertEqual((1, 1, 1, 1), (
            warm.ready_5m, warm.ready_20m, warm.ready_60m, warm.ready_120m
        ))

    def test_completed_snapshot_lifecycle_and_immutability(self) -> None:
        runtime = self.make_runtime()
        self.assertEqual("unavailable", runtime.completed_top_snapshot().status)

        self.add_history(runtime, lambda minute: 100 + minute)
        first_cycle = cycle_result()
        runtime._refresh_completed_top_snapshot(first_cycle, ("BTCUSDT",))
        first_access = runtime.completed_top_snapshot()

        self.assertEqual("ready", first_access.status)
        self.assertIsNotNone(first_access.snapshot)
        assert first_access.snapshot is not None
        self.assertEqual(1, first_access.snapshot.ready_20m)
        with self.assertRaises(FrozenInstanceError):
            first_access.snapshot.symbol_count = 2  # type: ignore[misc]

        later = NOW + timedelta(seconds=30)
        runtime.rolling_store.add(
            RollingOISample("BTCUSDT", 250.0, later, later)
        )
        runtime._clock = lambda: later
        second_cycle = replace(
            first_cycle,
            cycle_started_at_utc=later - timedelta(seconds=1),
            cycle_finished_at_utc=later,
        )
        runtime._refresh_completed_top_snapshot(second_cycle, ("BTCUSDT",))
        second_access = runtime.completed_top_snapshot()

        self.assertEqual("ready", second_access.status)
        self.assertIsNot(first_access.snapshot, second_access.snapshot)
        self.assertEqual(later, second_access.source_cycle_utc)

    def test_incomplete_cycles_retain_previous_completed_snapshot(self) -> None:
        runtime = self.make_runtime()
        self.add_history(runtime, lambda minute: 100 + minute)
        good_cycle = cycle_result()
        runtime._refresh_completed_top_snapshot(good_cycle, ("BTCUSDT",))
        good_snapshot = runtime.completed_top_snapshot().snapshot

        incomplete_cycles = (
            replace(good_cycle, failed_symbols=1, successful_samples=0),
            replace(
                good_cycle,
                cycle_timed_out=True,
                timed_out_symbols=1,
                successful_samples=0,
            ),
            replace(
                good_cycle,
                cycle_skipped=True,
                skip_reason="rate_budget",
                successful_samples=0,
            ),
            replace(good_cycle, successful_samples=0),
            replace(
                good_cycle,
                symbols_requested=2,
                successful_samples=2,
                oi_requests_attempted=2,
            ),
        )
        for incomplete in incomplete_cycles:
            with self.subTest(cycle=incomplete):
                runtime._refresh_completed_top_snapshot(
                    incomplete, ("BTCUSDT",)
                )
                self.assertIs(
                    good_snapshot, runtime.completed_top_snapshot().snapshot
                )

    def test_smooth_and_impulse_growth_both_expose_quality_metrics(self) -> None:
        for name, quantity_at in (
            ("smooth", lambda minute: 100 + minute * 0.1),
            ("impulse", lambda minute: 100 if minute < 120 else 112),
        ):
            with self.subTest(name=name):
                runtime = self.make_runtime()
                self.add_history(runtime, quantity_at)
                evaluation = runtime.evaluate_and_log(cycle_result())
                metrics = runtime.analyzer.analyze_120m(
                    runtime.rolling_store, "BTCUSDT"
                )

                self.assertEqual(1, evaluation.candidates_120m)
                self.assertTrue(metrics.available)
                self.assertIsNotNone(metrics.persistence)
                self.assertIsNotNone(metrics.trend_efficiency)
                self.assertIsNotNone(metrics.max_drawdown_pct)
                self.assertIsNotNone(metrics.impulse_concentration)
                self.assertGreater(metrics.coverage_ratio, 0)

    def test_candidate_and_summary_diagnostics_are_bounded(self) -> None:
        runtime = self.make_runtime()
        self.add_history(runtime, lambda minute: 100 + minute)

        with self.assertLogs("oitgbot.rolling.runtime", level="INFO") as captured:
            runtime.evaluate_and_log(cycle_result())

        output = "\n".join(captured.output)
        self.assertIn("ROLLING_SHADOW_SUMMARY", output)
        self.assertIn("ROLLING_OI_SHADOW", output)
        self.assertIn("SLOW_ACCUMULATION_SHADOW", output)
        self.assertIn("LONG_ACCUMULATION_SHADOW", output)

    def test_shadow_runtime_has_no_telegram_dependency_or_send_path(self) -> None:
        runtime = self.make_runtime()
        self.add_history(runtime, lambda minute: 100 + minute)

        with patch.object(TelegramSender, "send_if_not_empty") as send:
            runtime.evaluate_and_log(cycle_result())

        self.assertNotIn("telegram", vars(runtime))
        send.assert_not_called()

    def test_signal_transitions_are_counted_without_persistent_duplicates(self) -> None:
        runtime = self.make_runtime()
        for symbol, latest_quantity in (
            ("POSUSDT", 105.0),
            ("NEGUSDT", 95.0),
            ("PERSISTUSDT", 106.0),
        ):
            runtime.rolling_store.add(
                RollingOISample(
                    symbol,
                    100.0,
                    NOW - timedelta(minutes=5),
                    NOW - timedelta(minutes=5),
                )
            )
            runtime.rolling_store.add(
                RollingOISample(symbol, latest_quantity, NOW, NOW)
            )

        persistent = runtime.calculator.calculate_5m(
            runtime.rolling_store, "PERSISTUSDT"
        )
        runtime.signal_state_machine.evaluate(persistent)

        with self.assertLogs("oitgbot.rolling.runtime", level="INFO") as captured:
            evaluation = runtime.evaluate_and_log(cycle_result(requested=3, successful=3))

        self.assertEqual(1, evaluation.new_positive_triggers)
        self.assertEqual(1, evaluation.new_negative_triggers)
        self.assertEqual(0, evaluation.rearmed_positive)
        self.assertEqual(0, evaluation.rearmed_negative)
        self.assertEqual(2, evaluation.active_positive_states)
        self.assertEqual(1, evaluation.active_negative_states)
        output = "\n".join(captured.output)
        self.assertEqual(2, output.count("ROLLING_SIGNAL event=TRIGGER"))
        self.assertIn("new_positive_triggers=1", output)
        self.assertIn("new_negative_triggers=1", output)
