from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from unittest import TestCase

from oitgbot.models import RollingOISample
from oitgbot.services.rolling_oi_calculator import (
    AccumulationAnalyzer,
    RollingOICalculator,
)
from oitgbot.services.rolling_oi_store import RollingOIStore


START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def sample(
    minute: float,
    quantity: float,
    *,
    symbol: str = "BTCUSDT",
    price: float | None = None,
    oi_exchange_time: datetime | None = None,
) -> RollingOISample:
    observed_at = START + timedelta(minutes=minute)
    return RollingOISample(
        symbol=symbol,
        oi_quantity=quantity,
        observed_at_utc=observed_at,
        oi_exchange_time=oi_exchange_time or observed_at,
        mark_price=price,
        price_exchange_time=observed_at if price is not None else None,
    )


def populated_store(
    points: list[tuple[float, float]],
    *,
    symbol: str = "BTCUSDT",
) -> RollingOIStore:
    store = RollingOIStore()
    for minute, quantity in points:
        store.add(sample(minute, quantity, symbol=symbol))
    return store


class RollingOISampleTests(TestCase):
    def test_quantity_only_sample_is_valid(self) -> None:
        value = sample(0, 100)

        self.assertEqual(100.0, value.oi_quantity)
        self.assertIsNone(value.mark_price)
        self.assertIsNone(value.oi_value_usd)

    def test_mark_price_derives_usd_value(self) -> None:
        value = sample(0, 100, price=25)

        self.assertEqual(2_500.0, value.oi_value_usd)

    def test_rejects_missing_symbol_and_invalid_quantity(self) -> None:
        with self.assertRaises(ValueError):
            sample(0, 100, symbol="")
        for quantity in (float("nan"), float("inf"), -1):
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                sample(0, quantity)

    def test_rejects_naive_timestamps(self) -> None:
        with self.assertRaisesRegex(ValueError, "oi_exchange_time"):
            RollingOISample(
                symbol="BTCUSDT",
                oi_quantity=1,
                observed_at_utc=START,
                oi_exchange_time=datetime(2024, 1, 1),
            )
        with self.assertRaisesRegex(ValueError, "observed_at_utc"):
            RollingOISample(
                symbol="BTCUSDT",
                oi_quantity=1,
                observed_at_utc=datetime(2024, 1, 1),
                oi_exchange_time=START,
            )

    def test_rejects_invalid_optional_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "mark_price"):
            RollingOISample(
                symbol="BTCUSDT",
                oi_quantity=1,
                observed_at_utc=START,
                oi_exchange_time=START,
                mark_price=0,
            )
        with self.assertRaisesRegex(ValueError, "oi_value_usd"):
            RollingOISample(
                symbol="BTCUSDT",
                oi_quantity=1,
                observed_at_utc=START,
                oi_exchange_time=START,
                oi_value_usd=math.inf,
            )


class RollingOIStoreTests(TestCase):
    def test_first_and_chronological_insertions(self) -> None:
        store = RollingOIStore()

        self.assertTrue(store.add(sample(0, 100)))
        self.assertTrue(store.add(sample(0.5, 101)))
        self.assertEqual(
            [100.0, 101.0],
            [x.oi_quantity for x in store.history("BTCUSDT")],
        )

    def test_duplicate_with_richer_consistent_context_replaces(self) -> None:
        store = RollingOIStore()
        original = sample(0, 100)
        richer = sample(0, 100, price=25)

        store.add(original)

        self.assertTrue(store.add(richer))
        self.assertIs(richer, store.latest("BTCUSDT"))
        self.assertEqual(1, len(store.history("BTCUSDT")))

    def test_conflicting_duplicate_is_ignored(self) -> None:
        store = RollingOIStore()
        original = sample(0, 100)
        store.add(original)

        self.assertFalse(store.add(sample(0, 101, price=25)))
        self.assertIs(original, store.latest("BTCUSDT"))

    def test_out_of_order_sample_is_ignored(self) -> None:
        store = RollingOIStore()
        latest = sample(1, 101)
        store.add(latest)

        self.assertFalse(store.add(sample(0, 100)))
        self.assertEqual((latest,), store.history("BTCUSDT"))

    def test_latest_lookup_and_missing_symbol(self) -> None:
        store = populated_store([(0, 100), (1, 101)])

        self.assertEqual(101.0, store.latest("BTCUSDT").oi_quantity)  # type: ignore[union-attr]
        self.assertIsNone(store.latest("ETHUSDT"))

    def test_history_snapshot_cannot_mutate_store(self) -> None:
        store = populated_store([(0, 100), (1, 101)])
        history = store.history("BTCUSDT")

        self.assertIsInstance(history, tuple)
        changed = history + (sample(2, 102),)

        self.assertEqual(3, len(changed))
        self.assertEqual(2, len(store.history("BTCUSDT")))

    def test_retention_prunes_by_latest_observation_time(self) -> None:
        store = RollingOIStore(retention_minutes=150, max_samples_per_symbol=1_000)
        store.add(sample(0, 100))
        store.add(sample(150, 101))
        store.add(sample(151, 102))

        self.assertEqual([101.0, 102.0], [x.oi_quantity for x in store.history("BTCUSDT")])

    def test_default_and_explicit_hard_sample_bounds(self) -> None:
        default_store = RollingOIStore()
        self.assertEqual(302, default_store.max_samples_per_symbol)

        bounded_store = RollingOIStore(
            retention_minutes=150,
            max_samples_per_symbol=3,
        )
        for minute in range(4):
            bounded_store.add(sample(minute, 100 + minute))

        self.assertEqual(
            [101.0, 102.0, 103.0],
            [x.oi_quantity for x in bounded_store.history("BTCUSDT")],
        )

    def test_multiple_symbols_are_isolated(self) -> None:
        store = RollingOIStore()
        store.add(sample(0, 100, symbol="BTCUSDT"))
        store.add(sample(0, 200, symbol="ETHUSDT"))

        self.assertEqual(("BTCUSDT", "ETHUSDT"), store.symbols())
        self.assertEqual(100.0, store.latest("BTCUSDT").oi_quantity)  # type: ignore[union-attr]
        self.assertEqual(200.0, store.latest("ETHUSDT").oi_quantity)  # type: ignore[union-attr]

    def test_rejects_non_sample_input(self) -> None:
        store = RollingOIStore()

        with self.assertRaises(TypeError):
            store.add(object())  # type: ignore[arg-type]

    def test_repeated_transaction_timestamp_is_not_a_duplicate_observation(self) -> None:
        store = RollingOIStore()
        transaction_time = START - timedelta(minutes=2)

        self.assertTrue(
            store.add(sample(0, 100, oi_exchange_time=transaction_time))
        )
        self.assertTrue(
            store.add(sample(0.5, 100, oi_exchange_time=transaction_time))
        )

        history = store.history("BTCUSDT")
        self.assertEqual(2, len(history))
        self.assertEqual(transaction_time, history[0].oi_exchange_time)
        self.assertEqual(transaction_time, history[1].oi_exchange_time)


class RollingOICalculatorTests(TestCase):
    def setUp(self) -> None:
        self.calculator = RollingOICalculator(cadence_seconds=30)

    def test_exact_5m_baseline(self) -> None:
        store = populated_store([(0, 100), (5, 110)])

        result = self.calculator.calculate_5m(store, "BTCUSDT")

        self.assertTrue(result.available)
        self.assertEqual(START, result.baseline_timestamp)
        self.assertEqual(10.0, result.oi_quantity_change_pct)
        self.assertEqual(300.0, result.actual_window_seconds)
        self.assertEqual(0.0, result.baseline_offset_seconds)

    def test_5m_window_uses_observations_when_transaction_time_never_changes(self) -> None:
        transaction_time = START - timedelta(minutes=3)
        store = RollingOIStore()
        for half_minute in range(11):
            store.add(
                sample(
                    half_minute / 2,
                    100 + half_minute,
                    oi_exchange_time=transaction_time,
                )
            )

        result = self.calculator.calculate_5m(store, "BTCUSDT")

        self.assertTrue(result.available)
        self.assertEqual(11, len(store.history("BTCUSDT")))
        self.assertEqual(START, result.baseline_timestamp)
        self.assertEqual(START + timedelta(minutes=5), result.latest_timestamp)
        self.assertEqual(10.0, result.oi_quantity_change_pct)

    def test_all_supported_windows_use_observation_time(self) -> None:
        transaction_time = START - timedelta(minutes=3)
        for minutes in (5, 20, 60, 120):
            with self.subTest(minutes=minutes):
                store = RollingOIStore()
                store.add(sample(0, 100, oi_exchange_time=transaction_time))
                store.add(
                    sample(
                        minutes,
                        110,
                        oi_exchange_time=transaction_time,
                    )
                )

                result = self.calculator.calculate(
                    store, "BTCUSDT", minutes * 60
                )

                self.assertTrue(result.available)
                self.assertEqual(minutes * 60, result.actual_window_seconds)
                self.assertEqual(10.0, result.oi_quantity_change_pct)

    def test_selects_baseline_before_target_not_future_side(self) -> None:
        store = populated_store([(0, 100), (1, 999), (5.5, 110)])

        result = self.calculator.calculate_5m(store, "BTCUSDT")

        self.assertTrue(result.available)
        self.assertEqual(START, result.baseline_timestamp)
        self.assertEqual(30.0, result.baseline_offset_seconds)
        self.assertEqual(10.0, result.oi_quantity_change_pct)

    def test_tolerance_boundary_is_accepted(self) -> None:
        store = populated_store([(0, 100), (6, 110)])

        result = self.calculator.calculate_5m(store, "BTCUSDT")

        self.assertTrue(result.available)
        self.assertEqual(60.0, result.baseline_offset_seconds)

    def test_baseline_outside_tolerance_is_unavailable(self) -> None:
        store = populated_store([(0, 100), (6 + 1 / 60, 110)])

        result = self.calculator.calculate_5m(store, "BTCUSDT")

        self.assertFalse(result.available)
        self.assertEqual("baseline outside tolerance", result.unavailable_reason)
        self.assertIsNone(result.oi_quantity_change_pct)

    def test_insufficient_history_is_unavailable(self) -> None:
        store = populated_store([(5, 110)])

        result = self.calculator.calculate_5m(store, "BTCUSDT")

        self.assertFalse(result.available)
        self.assertEqual("no baseline at or before target", result.unavailable_reason)

    def test_missing_symbol_is_unavailable(self) -> None:
        result = self.calculator.calculate_5m(RollingOIStore(), "MISSING")

        self.assertFalse(result.available)
        self.assertEqual("no samples", result.unavailable_reason)

    def test_zero_quantity_baseline_is_explicitly_unavailable(self) -> None:
        store = populated_store([(0, 0), (5, 10)])

        result = self.calculator.calculate_5m(store, "BTCUSDT")

        self.assertFalse(result.available)
        self.assertEqual("baseline OI quantity is zero", result.unavailable_reason)
        self.assertIsNone(result.oi_quantity_change_pct)

    def test_20m_calculation(self) -> None:
        store = populated_store([(0, 100), (20, 120)])

        result = self.calculator.calculate_20m(store, "BTCUSDT")

        self.assertTrue(result.available)
        self.assertEqual(20.0, result.oi_quantity_change_pct)

    def test_60m_calculation(self) -> None:
        store = populated_store([(0, 100), (60, 130)])

        result = self.calculator.calculate_60m(store, "BTCUSDT")

        self.assertTrue(result.available)
        self.assertEqual(30.0, result.oi_quantity_change_pct)

    def test_120m_calculation(self) -> None:
        store = populated_store([(0, 100), (120, 140)])

        result = self.calculator.calculate_120m(store, "BTCUSDT")

        self.assertTrue(result.available)
        self.assertEqual(40.0, result.oi_quantity_change_pct)

    def test_missing_price_does_not_invalidate_quantity_change(self) -> None:
        store = populated_store([(0, 100), (5, 110)])

        result = self.calculator.calculate_5m(store, "BTCUSDT")

        self.assertTrue(result.available)
        self.assertEqual(10.0, result.oi_quantity_change_pct)
        self.assertIsNone(result.price_change_pct)
        self.assertIsNone(result.oi_value_change_pct)

    def test_price_and_derived_usd_changes_are_independent_metrics(self) -> None:
        store = RollingOIStore()
        store.add(sample(0, 100, price=10))
        store.add(sample(5, 110, price=12))

        result = self.calculator.calculate_5m(store, "BTCUSDT")

        self.assertAlmostEqual(10.0, result.oi_quantity_change_pct or 0)
        self.assertAlmostEqual(20.0, result.price_change_pct or 0)
        self.assertAlmostEqual(32.0, result.oi_value_change_pct or 0)


class AccumulationAnalyzerTests(TestCase):
    def setUp(self) -> None:
        self.analyzer = AccumulationAnalyzer(RollingOICalculator())

    def test_smooth_accumulation_has_full_persistence_and_efficiency(self) -> None:
        store = populated_store([(minute, 100 + minute / 5) for minute in range(0, 61, 5)])

        result = self.analyzer.analyze_60m(store, "BTCUSDT")

        self.assertTrue(result.available)
        self.assertEqual(6, result.positive_blocks)
        self.assertEqual(6, result.valid_blocks)
        self.assertEqual(1.0, result.persistence)
        self.assertEqual(1.0, result.trend_efficiency)
        self.assertEqual("positive", result.trend_direction)
        self.assertEqual(1.0, result.coverage_ratio)

    def test_choppy_growth_has_lower_trend_efficiency(self) -> None:
        smooth = populated_store([(minute, 100 + minute / 10) for minute in range(0, 61, 10)])
        choppy_quantities = [100, 104, 101, 105, 102, 106, 107]
        choppy = populated_store(
            [(minute, quantity) for minute, quantity in zip(range(0, 61, 10), choppy_quantities)]
        )

        smooth_result = self.analyzer.analyze_60m(smooth, "BTCUSDT")
        choppy_result = self.analyzer.analyze_60m(choppy, "BTCUSDT")

        self.assertGreater(choppy_result.net_oi_change_pct or 0, 0)
        self.assertLess(
            choppy_result.trend_efficiency or 0,
            smooth_result.trend_efficiency or 0,
        )
        self.assertEqual("positive", choppy_result.trend_direction)

    def test_maximum_drawdown_is_positive_peak_to_trough_magnitude(self) -> None:
        quantities = [100, 105, 110, 104, 108, 109, 111]
        store = populated_store(
            [(minute, quantity) for minute, quantity in zip(range(0, 61, 10), quantities)]
        )

        result = self.analyzer.analyze_60m(store, "BTCUSDT")

        self.assertAlmostEqual((110 - 104) / 110 * 100, result.max_drawdown_pct or 0)

    def test_impulse_dominated_growth_has_higher_concentration(self) -> None:
        distributed = populated_store(
            [(minute, 100 + index) for index, minute in enumerate(range(0, 61, 5))]
        )
        dominated_quantities = [100, 101, 102, 103, 104, 105, 120, 121, 122, 123, 124, 125, 126]
        dominated = populated_store(
            [(minute, quantity) for minute, quantity in zip(range(0, 61, 5), dominated_quantities)]
        )

        distributed_result = self.analyzer.analyze_60m(distributed, "BTCUSDT")
        dominated_result = self.analyzer.analyze_60m(dominated, "BTCUSDT")

        self.assertGreater(
            dominated_result.impulse_concentration or 0,
            distributed_result.impulse_concentration or 0,
        )
        self.assertGreater(
            dominated_result.max_5m_change_pct or 0,
            distributed_result.max_5m_change_pct or 0,
        )

    def test_no_positive_movement_has_unavailable_concentration(self) -> None:
        store = populated_store(
            [(minute, 100 - index) for index, minute in enumerate(range(0, 61, 5))]
        )

        result = self.analyzer.analyze_60m(store, "BTCUSDT")

        self.assertIsNone(result.impulse_concentration)
        self.assertEqual("negative", result.trend_direction)

    def test_flat_path_has_neutral_efficiency(self) -> None:
        store = populated_store(
            [(minute, 100) for minute in range(0, 61, 5)]
        )

        result = self.analyzer.analyze_60m(store, "BTCUSDT")

        self.assertEqual(0.0, result.trend_efficiency)
        self.assertEqual("flat", result.trend_direction)
        self.assertEqual(6, result.flat_blocks)
        self.assertIsNone(result.impulse_concentration)

    def test_missing_anchors_reduce_coverage_without_becoming_negative(self) -> None:
        points = [
            (minute, 100 + minute)
            for minute in range(0, 61, 5)
            if minute != 20
        ]
        store = populated_store(points)

        result = self.analyzer.analyze_60m(store, "BTCUSDT")

        self.assertTrue(result.available)
        self.assertEqual(4, result.valid_blocks)
        self.assertEqual(4, result.positive_blocks)
        self.assertEqual(0, result.negative_blocks)
        self.assertAlmostEqual(4 / 6, result.coverage_ratio)

    def test_120m_metrics_use_twelve_expected_blocks(self) -> None:
        store = populated_store(
            [(minute, 100 + minute / 10) for minute in range(0, 121, 10)]
        )

        result = self.analyzer.analyze_120m(store, "BTCUSDT")

        self.assertTrue(result.available)
        self.assertEqual(12, result.expected_blocks)
        self.assertEqual(12, result.valid_blocks)
        self.assertEqual(1.0, result.coverage_ratio)

    def test_unavailable_exact_window_produces_unavailable_quality(self) -> None:
        store = populated_store([(60, 110)])

        result = self.analyzer.analyze_60m(store, "BTCUSDT")

        self.assertFalse(result.available)
        self.assertEqual(0.0, result.coverage_ratio)
        self.assertIsNone(result.persistence)
