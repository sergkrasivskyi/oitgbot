from __future__ import annotations

from unittest import TestCase

from oitgbot.models import BinanceRateLimit
from oitgbot.services.rate_limit_budget import (
    BudgetState,
    RateLimitBudget,
    estimate_cadence,
)


def request_weight_limit(
    limit: int,
    *,
    interval: str = "MINUTE",
    interval_num: int = 1,
) -> BinanceRateLimit:
    return BinanceRateLimit("REQUEST_WEIGHT", interval, interval_num, limit)


class RateLimitBudgetTests(TestCase):
    def test_valid_one_minute_request_weight_limit(self) -> None:
        budget = RateLimitBudget(
            [request_weight_limit(1_000)], retry_allowance_ratio=0
        )

        decision = budget.evaluate_cycle(100, 60)

        self.assertEqual(BudgetState.SAFE, decision.state)
        self.assertEqual(1, len(decision.projections))
        self.assertEqual(1_000, decision.projections[0].runtime_limit)

    def test_multiple_request_weight_windows_are_all_preserved(self) -> None:
        budget = RateLimitBudget(
            [
                request_weight_limit(1_000, interval="MINUTE"),
                request_weight_limit(250, interval="SECOND", interval_num=10),
            ],
            retry_allowance_ratio=0,
        )

        decision = budget.evaluate_cycle(100, 60)

        self.assertEqual(2, len(decision.projections))
        self.assertEqual(
            {"MINUTE", "SECOND"},
            {projection.interval for projection in decision.projections},
        )

    def test_most_restrictive_window_controls_decision(self) -> None:
        budget = RateLimitBudget(
            [
                request_weight_limit(10_000, interval="MINUTE"),
                request_weight_limit(100, interval="SECOND", interval_num=10),
            ],
            retry_allowance_ratio=0,
        )

        decision = budget.evaluate_cycle(80, 60)

        self.assertEqual(BudgetState.UNSAFE, decision.state)

    def test_non_request_weight_limits_are_ignored(self) -> None:
        budget = RateLimitBudget(
            [
                BinanceRateLimit("ORDERS", "MINUTE", 1, 1),
                request_weight_limit(1_000),
            ],
            retry_allowance_ratio=0,
        )

        decision = budget.evaluate_cycle(100, 60)

        self.assertEqual(BudgetState.SAFE, decision.state)
        self.assertEqual(1, len(decision.projections))

    def test_thirty_percent_reserve_is_enforced(self) -> None:
        budget = RateLimitBudget(
            [request_weight_limit(1_000)], retry_allowance_ratio=0
        )

        at_usable_limit = budget.evaluate_cycle(700, 60)
        above_usable_limit = budget.evaluate_cycle(701, 60)

        self.assertEqual(700.0, at_usable_limit.projections[0].usable_limit)
        self.assertEqual(BudgetState.PRESSURE, at_usable_limit.state)
        self.assertEqual(BudgetState.UNSAFE, above_usable_limit.state)

    def test_safe_pressure_and_unsafe_states(self) -> None:
        budget = RateLimitBudget(
            [request_weight_limit(1_000)], retry_allowance_ratio=0
        )

        self.assertEqual(BudgetState.SAFE, budget.evaluate_cycle(500, 60).state)
        self.assertEqual(
            BudgetState.PRESSURE, budget.evaluate_cycle(600, 60).state
        )
        self.assertEqual(
            BudgetState.UNSAFE, budget.evaluate_cycle(701, 60).state
        )

    def test_cadence_estimate_at_15_seconds(self) -> None:
        estimate = estimate_cadence(500, 15)

        self.assertEqual(4.0, estimate.cycles_per_minute)
        self.assertEqual(2_000.0, estimate.oi_calls_per_minute)

    def test_cadence_estimate_at_20_seconds(self) -> None:
        estimate = estimate_cadence(500, 20)

        self.assertEqual(3.0, estimate.cycles_per_minute)
        self.assertEqual(1_500.0, estimate.base_request_weight_per_minute)

    def test_cadence_estimate_at_30_seconds(self) -> None:
        estimate = estimate_cadence(550, 30)

        self.assertEqual(2.0, estimate.cycles_per_minute)
        self.assertEqual(1_100.0, estimate.oi_calls_per_minute)

    def test_cadence_estimate_at_60_seconds(self) -> None:
        estimate = estimate_cadence(550, 60)

        self.assertEqual(1.0, estimate.cycles_per_minute)
        self.assertEqual(550.0, estimate.base_request_weight_per_minute)

    def test_retry_allowance_is_included(self) -> None:
        budget = RateLimitBudget(
            [request_weight_limit(10_000)], retry_allowance_ratio=0.10
        )

        decision = budget.evaluate_cycle(100, 60)

        self.assertEqual(10, decision.retry_reserve_per_cycle)
        self.assertEqual(110.0, decision.projections[0].projected_weight)

    def test_other_rest_reserve_is_included(self) -> None:
        budget = RateLimitBudget(
            [request_weight_limit(10_000)],
            retry_allowance_ratio=0,
            other_rest_reserve_per_minute=75,
        )

        decision = budget.evaluate_cycle(100, 60)

        self.assertEqual(175.0, decision.projections[0].projected_weight)

    def test_missing_request_weight_limits_is_explicitly_unsafe(self) -> None:
        budget = RateLimitBudget(
            [BinanceRateLimit("ORDERS", "MINUTE", 1, 1_000)]
        )

        decision = budget.evaluate_cycle(100, 60)

        self.assertEqual(BudgetState.UNSAFE, decision.state)
        self.assertEqual("missing_request_weight_limits", decision.reason)
        self.assertEqual((), decision.projections)

    def test_unknown_runtime_interval_is_rejected(self) -> None:
        budget = RateLimitBudget(
            [request_weight_limit(1_000, interval="WEEK")]
        )

        with self.assertRaisesRegex(ValueError, "unsupported"):
            budget.evaluate_cycle(100, 60)


class CadenceWorkloadExamplesTests(TestCase):
    def test_500_and_550_symbol_base_workloads(self) -> None:
        expected = {
            15: (2_000.0, 2_200.0),
            20: (1_500.0, 1_650.0),
            30: (1_000.0, 1_100.0),
            60: (500.0, 550.0),
        }

        for cadence, workloads in expected.items():
            with self.subTest(cadence=cadence):
                self.assertEqual(
                    workloads,
                    (
                        estimate_cadence(500, cadence).oi_calls_per_minute,
                        estimate_cadence(550, cadence).oi_calls_per_minute,
                    ),
                )
