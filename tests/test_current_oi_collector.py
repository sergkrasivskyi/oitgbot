from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from unittest import IsolatedAsyncioTestCase

import requests

from oitgbot.models import BinanceRateLimit, CurrentOpenInterest, MarkPriceUpdate
from oitgbot.services.current_oi_collector import CurrentOICollector
from oitgbot.services.price_state import PriceStateStore
from oitgbot.services.rate_limit_budget import RateLimitBudget
from oitgbot.services.rolling_oi_store import RollingOIStore


NOW = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)


class FakeBinanceAPI:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.symbol_discovery_calls = 0
        self._lock = threading.Lock()

    def get_current_open_interest(self, symbol: str) -> CurrentOpenInterest:
        with self._lock:
            self.calls.append(symbol)
        response = self.responses[symbol]
        if callable(response):
            response = response()
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]

    def get_perpetual_futures_symbols(self) -> list[str]:
        self.symbol_discovery_calls += 1
        return list(self.responses)


def current_oi(
    symbol: str,
    quantity: float,
    exchange_time: datetime | None = None,
) -> CurrentOpenInterest:
    return CurrentOpenInterest(symbol, quantity, exchange_time or NOW)


def generous_budget() -> RateLimitBudget:
    return RateLimitBudget(
        [BinanceRateLimit("REQUEST_WEIGHT", "MINUTE", 1, 1_000_000)],
        retry_allowance_ratio=0,
    )


def add_price(
    store: PriceStateStore,
    symbol: str,
    price: float,
    *,
    exchange_time: datetime = NOW,
    received_at: datetime = NOW,
) -> None:
    store.update(
        MarkPriceUpdate(
            symbol=symbol,
            mark_price=price,
            exchange_time=exchange_time,
            received_at_utc=received_at,
        )
    )


def http_error(status_code: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    error = requests.HTTPError(f"HTTP {status_code}")
    error.response = response
    return error


class CurrentOICollectorTests(IsolatedAsyncioTestCase):
    async def test_happy_path_collects_enriches_and_stores_multiple_symbols(self) -> None:
        api = FakeBinanceAPI(
            {
                "BTCUSDT": current_oi("BTCUSDT", 100, NOW - timedelta(seconds=1)),
                "ETHUSDT": current_oi("ETHUSDT", 200, NOW - timedelta(seconds=2)),
            }
        )
        prices = PriceStateStore()
        add_price(prices, "BTCUSDT", 50_000, received_at=NOW - timedelta(seconds=1))
        add_price(prices, "ETHUSDT", 2_000, received_at=NOW - timedelta(seconds=1))
        rolling = RollingOIStore()
        collector = CurrentOICollector(
            api, prices, rolling, generous_budget(), clock=lambda: NOW
        )
        self.addAsyncCleanup(collector.close)

        result = await collector.collect_cycle(["BTCUSDT", "ETHUSDT"])

        self.assertEqual(2, result.symbols_requested)
        self.assertEqual(2, result.oi_requests_attempted)
        self.assertEqual(2, result.successful_samples)
        self.assertEqual(2, result.samples_inserted)
        self.assertEqual(2, result.price_fresh)
        self.assertEqual(0, result.failed_symbols)
        btc = rolling.latest("BTCUSDT")
        assert btc is not None
        self.assertEqual(NOW - timedelta(seconds=1), btc.oi_exchange_time)
        self.assertEqual(NOW, btc.observed_at_utc)
        self.assertEqual(50_000.0, btc.mark_price)
        self.assertEqual(5_000_000.0, btc.oi_value_usd)

    async def test_fresh_missing_and_stale_prices_all_keep_quantity(self) -> None:
        symbols = ("FRESHUSDT", "MISSINGUSDT", "STALEUSDT")
        api = FakeBinanceAPI(
            {symbol: current_oi(symbol, 100, NOW) for symbol in symbols}
        )
        prices = PriceStateStore()
        add_price(prices, "FRESHUSDT", 10, received_at=NOW - timedelta(seconds=1))
        add_price(prices, "STALEUSDT", 20, received_at=NOW - timedelta(seconds=6))
        rolling = RollingOIStore()
        collector = CurrentOICollector(
            api, prices, rolling, generous_budget(), clock=lambda: NOW
        )
        self.addAsyncCleanup(collector.close)

        result = await collector.collect_cycle(symbols)

        self.assertEqual(3, result.samples_inserted)
        self.assertEqual(1, result.price_fresh)
        self.assertEqual(1, result.price_missing)
        self.assertEqual(1, result.price_receipt_stale)
        self.assertEqual(0, result.price_alignment_rejected)
        self.assertIsNotNone(rolling.latest("FRESHUSDT").mark_price)  # type: ignore[union-attr]
        self.assertIsNone(rolling.latest("MISSINGUSDT").mark_price)  # type: ignore[union-attr]
        self.assertIsNone(rolling.latest("STALEUSDT").mark_price)  # type: ignore[union-attr]

    async def test_excessive_price_observation_skew_removes_only_price_context(self) -> None:
        api = FakeBinanceAPI({"BTCUSDT": current_oi("BTCUSDT", 100, NOW)})
        prices = PriceStateStore()
        add_price(
            prices,
            "BTCUSDT",
            50_000,
            exchange_time=NOW - timedelta(seconds=6),
            received_at=NOW,
        )
        rolling = RollingOIStore()
        collector = CurrentOICollector(
            api, prices, rolling, generous_budget(), clock=lambda: NOW
        )
        self.addAsyncCleanup(collector.close)

        result = await collector.collect_cycle(["BTCUSDT"])

        self.assertEqual(1, result.samples_inserted)
        self.assertEqual(1, result.price_alignment_rejected)
        self.assertEqual(6.0, result.price_event_age_abs_max_s)
        self.assertIsNone(rolling.latest("BTCUSDT").mark_price)  # type: ignore[union-attr]

    async def test_old_oi_transaction_time_does_not_reject_current_price(self) -> None:
        api = FakeBinanceAPI(
            {
                "BTCUSDT": current_oi(
                    "BTCUSDT", 100, NOW - timedelta(minutes=2)
                )
            }
        )
        prices = PriceStateStore()
        add_price(prices, "BTCUSDT", 50_000, exchange_time=NOW, received_at=NOW)
        rolling = RollingOIStore()
        collector = CurrentOICollector(
            api, prices, rolling, generous_budget(), clock=lambda: NOW
        )
        self.addAsyncCleanup(collector.close)

        result = await collector.collect_cycle(["BTCUSDT"])

        sample = rolling.latest("BTCUSDT")
        assert sample is not None
        self.assertEqual(1, result.samples_inserted)
        self.assertEqual(1, result.old_transaction_time_count)
        self.assertEqual(1, result.price_fresh)
        self.assertEqual(120.0, result.price_oi_transaction_skew_abs_max_s)
        self.assertEqual(50_000.0, sample.mark_price)

    async def test_repeated_transaction_time_retains_each_observation(self) -> None:
        class MutableClock:
            current = NOW

            def __call__(self) -> datetime:
                return self.current

        clock = MutableClock()
        api = FakeBinanceAPI(
            {"BTCUSDT": current_oi("BTCUSDT", 100, NOW)}
        )
        rolling = RollingOIStore()
        collector = CurrentOICollector(
            api,
            PriceStateStore(),
            rolling,
            generous_budget(),
            clock=clock,
        )
        self.addAsyncCleanup(collector.close)

        first = await collector.collect_cycle(["BTCUSDT"])
        clock.current = NOW + timedelta(seconds=30)
        second = await collector.collect_cycle(["BTCUSDT"])

        self.assertEqual(1, first.samples_inserted)
        self.assertEqual(1, second.samples_inserted)
        self.assertEqual(1, second.transaction_time_unchanged)
        self.assertEqual(2, len(rolling.history("BTCUSDT")))
        self.assertEqual(
            [NOW, NOW + timedelta(seconds=30)],
            [sample.observed_at_utc for sample in rolling.history("BTCUSDT")],
        )

    async def test_old_transaction_is_accepted_and_impossible_future_is_rejected(self) -> None:
        api = FakeBinanceAPI(
            {
                "FRESHUSDT": current_oi("FRESHUSDT", 1, NOW - timedelta(seconds=60)),
                "OLDUSDT": current_oi("OLDUSDT", 1, NOW - timedelta(seconds=120)),
                "SMALLFUTUREUSDT": current_oi("SMALLFUTUREUSDT", 1, NOW + timedelta(seconds=5)),
                "FUTUREUSDT": current_oi("FUTUREUSDT", 1, NOW + timedelta(seconds=6)),
            }
        )
        rolling = RollingOIStore()
        collector = CurrentOICollector(
            api,
            PriceStateStore(),
            rolling,
            generous_budget(),
            clock=lambda: NOW,
        )
        self.addAsyncCleanup(collector.close)

        with self.assertLogs(
            "oitgbot.rolling.collector", level="INFO"
        ) as captured:
            result = await collector.collect_cycle(api.responses)

        self.assertEqual(3, result.samples_inserted)
        self.assertEqual(1, result.old_transaction_time_count)
        self.assertEqual(1, result.future_oi_rejected)
        self.assertEqual(-6.0, result.transaction_age_min_s)
        self.assertEqual(27.5, result.transaction_age_median_s)
        self.assertEqual(120.0, result.transaction_age_p95_s)
        self.assertEqual(120.0, result.transaction_age_max_s)
        self.assertIsNotNone(rolling.latest("FRESHUSDT"))
        self.assertIsNotNone(rolling.latest("SMALLFUTUREUSDT"))
        self.assertIsNotNone(rolling.latest("OLDUSDT"))
        self.assertIsNone(rolling.latest("FUTUREUSDT"))
        summary = "\n".join(captured.output)
        self.assertIn("transaction_age_median_s=27.500", summary)
        self.assertIn("transaction_age_p95_s=120.000", summary)
        self.assertIn("price_receipt_stale=0", summary)
        self.assertIn("price_alignment_rejected=0", summary)

    async def test_partial_failure_preserves_successes_and_old_transaction(self) -> None:
        responses: dict[str, object] = {
            f"S{i}USDT": current_oi(f"S{i}USDT", 100 + i, NOW)
            for i in range(8)
        }
        responses["ERRORUSDT"] = ConnectionError("request failed")
        responses["STALEUSDT"] = current_oi(
            "STALEUSDT", 100, NOW - timedelta(seconds=61)
        )
        api = FakeBinanceAPI(responses)
        rolling = RollingOIStore()
        collector = CurrentOICollector(
            api,
            PriceStateStore(),
            rolling,
            generous_budget(),
            clock=lambda: NOW,
        )
        self.addAsyncCleanup(collector.close)

        result = await collector.collect_cycle(responses)

        self.assertEqual(9, result.successful_samples)
        self.assertEqual(9, result.samples_inserted)
        self.assertEqual(1, result.failed_symbols)
        self.assertEqual(9, len(rolling.symbols()))
        self.assertEqual(1, result.old_transaction_time_count)
        self.assertEqual(
            {"request_error": 1}, dict(result.failure_counts)
        )

    async def test_malformed_client_model_is_isolated(self) -> None:
        api = FakeBinanceAPI(
            {
                "GOODUSDT": current_oi("GOODUSDT", 100, NOW),
                "BADUSDT": {"symbol": "BADUSDT", "openInterest": "100"},
            }
        )
        rolling = RollingOIStore()
        collector = CurrentOICollector(
            api,
            PriceStateStore(),
            rolling,
            generous_budget(),
            clock=lambda: NOW,
        )
        self.addAsyncCleanup(collector.close)

        result = await collector.collect_cycle(api.responses)

        self.assertEqual(1, result.samples_inserted)
        self.assertEqual({"parse_error": 1}, dict(result.failure_counts))
        self.assertIsNotNone(rolling.latest("GOODUSDT"))
        self.assertIsNone(rolling.latest("BADUSDT"))

    async def test_configured_worker_count_bounds_concurrency(self) -> None:
        release = threading.Event()
        two_started = threading.Event()
        state_lock = threading.Lock()
        active = 0
        peak_active = 0

        def bounded_response(symbol: str) -> CurrentOpenInterest:
            nonlocal active, peak_active
            with state_lock:
                active += 1
                peak_active = max(peak_active, active)
                if active == 2:
                    two_started.set()
            release.wait(1)
            with state_lock:
                active -= 1
            return current_oi(symbol, 100, NOW)

        responses = {
            f"S{i}USDT": (lambda symbol=f"S{i}USDT": bounded_response(symbol))
            for i in range(6)
        }
        api = FakeBinanceAPI(responses)
        collector = CurrentOICollector(
            api,
            PriceStateStore(),
            RollingOIStore(),
            generous_budget(),
            max_workers=2,
            clock=lambda: NOW,
        )
        self.addAsyncCleanup(collector.close)
        cycle = asyncio.create_task(collector.collect_cycle(responses))
        await asyncio.to_thread(two_started.wait, 1)

        self.assertEqual(2, len(api.calls))
        release.set()
        result = await cycle

        self.assertEqual(2, peak_active)
        self.assertEqual(6, result.samples_inserted)

    async def test_overlapping_cycle_is_skipped(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def delayed_response() -> CurrentOpenInterest:
            started.set()
            release.wait(1)
            return current_oi("BTCUSDT", 100, NOW)

        api = FakeBinanceAPI({"BTCUSDT": delayed_response})
        collector = CurrentOICollector(
            api,
            PriceStateStore(),
            RollingOIStore(),
            generous_budget(),
            max_workers=1,
            clock=lambda: NOW,
        )
        self.addAsyncCleanup(collector.close)
        first_cycle = asyncio.create_task(collector.collect_cycle(["BTCUSDT"]))
        await asyncio.to_thread(started.wait, 1)

        second_result = await collector.collect_cycle(["BTCUSDT"])

        self.assertTrue(second_result.cycle_skipped)
        self.assertEqual("cycle_already_running", second_result.skip_reason)
        self.assertEqual(1, len(api.calls))
        release.set()
        first_result = await first_cycle
        self.assertEqual(1, first_result.samples_inserted)

    async def test_unsafe_budget_starts_zero_oi_requests(self) -> None:
        api = FakeBinanceAPI(
            {f"S{i}USDT": current_oi(f"S{i}USDT", 1, NOW) for i in range(100)}
        )
        unsafe_budget = RateLimitBudget(
            [BinanceRateLimit("REQUEST_WEIGHT", "MINUTE", 1, 100)],
            retry_allowance_ratio=0,
        )
        collector = CurrentOICollector(
            api,
            PriceStateStore(),
            RollingOIStore(),
            unsafe_budget,
            clock=lambda: NOW,
        )
        self.addAsyncCleanup(collector.close)

        result = await collector.collect_cycle(api.responses, cadence_seconds=60)

        self.assertTrue(result.cycle_skipped)
        self.assertEqual("rate_budget_unsafe", result.skip_reason)
        self.assertEqual("UNSAFE", result.rate_budget_state)
        self.assertEqual(0, result.oi_requests_attempted)
        self.assertEqual([], api.calls)

    async def test_timeout_returns_partial_success_and_blocks_overlap_until_drain(self) -> None:
        release = threading.Event()

        def delayed_response() -> CurrentOpenInterest:
            release.wait(1)
            return current_oi("SLOWUSDT", 200, NOW)

        api = FakeBinanceAPI(
            {
                "FASTUSDT": current_oi("FASTUSDT", 100, NOW),
                "SLOWUSDT": delayed_response,
            }
        )
        rolling = RollingOIStore()
        collector = CurrentOICollector(
            api,
            PriceStateStore(),
            rolling,
            generous_budget(),
            max_workers=2,
            clock=lambda: NOW,
        )
        self.addAsyncCleanup(collector.close)

        result = await collector.collect_cycle(
            ["FASTUSDT", "SLOWUSDT"], timeout_seconds=0.05
        )

        self.assertTrue(result.cycle_timed_out)
        self.assertEqual(1, result.successful_samples)
        self.assertEqual(1, result.timed_out_symbols)
        self.assertIsNotNone(rolling.latest("FASTUSDT"))
        self.assertIsNone(rolling.latest("SLOWUSDT"))
        overlap = await collector.collect_cycle(["FASTUSDT"])
        self.assertEqual("cycle_already_running", overlap.skip_reason)
        release.set()

    async def test_duplicate_store_outcome_is_counted_without_failure(self) -> None:
        api = FakeBinanceAPI({"BTCUSDT": current_oi("BTCUSDT", 100, NOW)})
        rolling = RollingOIStore()
        collector = CurrentOICollector(
            api,
            PriceStateStore(),
            rolling,
            generous_budget(),
            clock=lambda: NOW,
        )
        self.addAsyncCleanup(collector.close)

        first = await collector.collect_cycle(["BTCUSDT"])
        second = await collector.collect_cycle(["BTCUSDT"])

        self.assertEqual(1, first.samples_inserted)
        self.assertEqual(0, second.samples_inserted)
        self.assertEqual(1, second.samples_ignored_duplicate_or_out_of_order)
        self.assertEqual(0, second.failed_symbols)

    async def test_empty_price_store_still_populates_quantity_history(self) -> None:
        api = FakeBinanceAPI({"BTCUSDT": current_oi("BTCUSDT", 100, NOW)})
        rolling = RollingOIStore()
        collector = CurrentOICollector(
            api,
            PriceStateStore(),
            rolling,
            generous_budget(),
            clock=lambda: NOW,
        )
        self.addAsyncCleanup(collector.close)

        result = await collector.collect_cycle(["BTCUSDT"])

        self.assertEqual(1, result.price_missing)
        self.assertEqual(1, result.samples_inserted)
        stored = rolling.latest("BTCUSDT")
        assert stored is not None
        self.assertEqual(100.0, stored.oi_quantity)
        self.assertIsNone(stored.mark_price)
        self.assertIsNone(stored.oi_value_usd)

    async def test_symbol_discovery_reuses_existing_binance_method(self) -> None:
        api = FakeBinanceAPI({"BTCUSDT": current_oi("BTCUSDT", 100, NOW)})
        rolling = RollingOIStore()
        collector = CurrentOICollector(
            api,
            PriceStateStore(),
            rolling,
            generous_budget(),
            clock=lambda: NOW,
        )
        self.addAsyncCleanup(collector.close)

        result = await collector.collect_cycle()

        self.assertEqual(1, api.symbol_discovery_calls)
        self.assertEqual(1, result.samples_inserted)

    async def test_http_429_and_418_remain_distinguishable(self) -> None:
        api = FakeBinanceAPI(
            {"RATEUSDT": http_error(429), "BANUSDT": http_error(418)}
        )
        collector = CurrentOICollector(
            api,
            PriceStateStore(),
            RollingOIStore(),
            generous_budget(),
            clock=lambda: NOW,
        )
        self.addAsyncCleanup(collector.close)

        result = await collector.collect_cycle(api.responses)

        self.assertEqual(1, result.http_429_errors)
        self.assertEqual(1, result.http_418_errors)
        self.assertEqual({"http_418": 1, "http_429": 1}, dict(result.failure_counts))

    async def test_close_is_idempotent_and_future_cycles_are_skipped(self) -> None:
        api = FakeBinanceAPI({"BTCUSDT": current_oi("BTCUSDT", 100, NOW)})
        collector = CurrentOICollector(
            api,
            PriceStateStore(),
            RollingOIStore(),
            generous_budget(),
            clock=lambda: NOW,
        )

        await collector.close()
        await collector.close()
        result = await collector.collect_cycle(["BTCUSDT"])

        self.assertTrue(result.cycle_skipped)
        self.assertEqual("collector_closed", result.skip_reason)

    async def test_close_waits_for_active_cycle_then_shuts_down(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def delayed_response() -> CurrentOpenInterest:
            started.set()
            release.wait(1)
            return current_oi("BTCUSDT", 100, NOW)

        api = FakeBinanceAPI({"BTCUSDT": delayed_response})
        collector = CurrentOICollector(
            api,
            PriceStateStore(),
            RollingOIStore(),
            generous_budget(),
            max_workers=1,
            clock=lambda: NOW,
        )
        cycle = asyncio.create_task(collector.collect_cycle(["BTCUSDT"]))
        await asyncio.to_thread(started.wait, 1)
        closing = asyncio.create_task(collector.close())
        await asyncio.sleep(0)

        self.assertFalse(closing.done())
        release.set()
        result = await cycle
        await closing

        self.assertEqual(1, result.samples_inserted)
        skipped = await collector.collect_cycle(["BTCUSDT"])
        self.assertEqual("collector_closed", skipped.skip_reason)
