from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import TestCase

from oitgbot.models import RollingOIWindowResult
from oitgbot.services.rolling_oi_signal_state import (
    RollingOISignalDirection,
    RollingOISignalEventType,
    RollingOISignalState,
    RollingOISignalStateMachine,
)


NOW = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)


def rolling_result(
    value: float | None,
    *,
    symbol: str = "BTCUSDT",
    available: bool = True,
    price_change_pct: float | None = 1.0,
    oi_value_change_pct: float | None = 6.0,
) -> RollingOIWindowResult:
    return RollingOIWindowResult(
        symbol=symbol,
        window_seconds=300,
        available=available,
        unavailable_reason=None if available else "no baseline",
        latest_timestamp=NOW,
        baseline_timestamp=NOW - timedelta(minutes=5),
        target_timestamp=NOW - timedelta(minutes=5),
        actual_window_seconds=300.0,
        baseline_offset_seconds=0.0,
        latest_oi_quantity=105.0,
        baseline_oi_quantity=100.0,
        oi_quantity_change_pct=value,
        latest_mark_price=101.0 if price_change_pct is not None else None,
        baseline_mark_price=100.0 if price_change_pct is not None else None,
        price_change_pct=price_change_pct,
        latest_oi_value_usd=10605.0 if oi_value_change_pct is not None else None,
        baseline_oi_value_usd=10000.0 if oi_value_change_pct is not None else None,
        oi_value_change_pct=oi_value_change_pct,
    )


class RollingOISignalConfigurationTests(TestCase):
    def test_defaults_and_hysteresis_validation(self) -> None:
        machine = RollingOISignalStateMachine()
        self.assertEqual(5.0, machine.trigger_threshold_pct)
        self.assertEqual(3.0, machine.rearm_threshold_pct)

        invalid = (
            (0, 0),
            (-1, 0),
            (5, -1),
            (5, 5),
            (5, 6),
            (float("nan"), 3),
            (5, float("inf")),
        )
        for trigger, rearm in invalid:
            with self.subTest(trigger=trigger, rearm=rearm):
                with self.assertRaises(ValueError):
                    RollingOISignalStateMachine(
                        trigger_threshold_pct=trigger,
                        rearm_threshold_pct=rearm,
                    )


class RollingOISignalStateMachineTests(TestCase):
    def test_initial_threshold_boundaries(self) -> None:
        cases = (
            (4.99, None, RollingOISignalState.NORMAL),
            (
                5.0,
                RollingOISignalDirection.POSITIVE,
                RollingOISignalState.POSITIVE_TRIGGERED,
            ),
            (
                6.0,
                RollingOISignalDirection.POSITIVE,
                RollingOISignalState.POSITIVE_TRIGGERED,
            ),
            (-4.99, None, RollingOISignalState.NORMAL),
            (
                -5.0,
                RollingOISignalDirection.NEGATIVE,
                RollingOISignalState.NEGATIVE_TRIGGERED,
            ),
            (
                -6.0,
                RollingOISignalDirection.NEGATIVE,
                RollingOISignalState.NEGATIVE_TRIGGERED,
            ),
        )
        for value, direction, state in cases:
            with self.subTest(value=value):
                machine = RollingOISignalStateMachine()
                event = machine.evaluate(rolling_result(value))
                self.assertEqual(direction, event.direction if event else None)
                self.assertEqual(state, machine.state_for("BTCUSDT").state)

    def test_persistent_positive_extreme_emits_only_once(self) -> None:
        machine = RollingOISignalStateMachine()
        events = [
            machine.evaluate(rolling_result(value))
            for value in (5.2, 5.8, 7.1, 4.0, 3.1)
        ]
        self.assertEqual(1, sum(event is not None for event in events))
        self.assertEqual(
            RollingOISignalState.POSITIVE_TRIGGERED,
            machine.state_for("BTCUSDT").state,
        )

    def test_positive_rearms_at_boundary_and_can_trigger_again(self) -> None:
        machine = RollingOISignalStateMachine()
        events = [
            machine.evaluate(rolling_result(value)) for value in (5.2, 4.4, 3.0, 5.1)
        ]
        self.assertEqual(
            [
                RollingOISignalEventType.TRIGGER,
                None,
                RollingOISignalEventType.REARM,
                RollingOISignalEventType.TRIGGER,
            ],
            [event.event_type if event else None for event in events],
        )

    def test_persistent_negative_extreme_emits_only_once(self) -> None:
        machine = RollingOISignalStateMachine()
        events = [
            machine.evaluate(rolling_result(value))
            for value in (-5.2, -5.8, -7.1, -4.0, -3.1)
        ]
        self.assertEqual(1, sum(event is not None for event in events))
        self.assertEqual(
            RollingOISignalState.NEGATIVE_TRIGGERED,
            machine.state_for("BTCUSDT").state,
        )

    def test_negative_rearms_at_boundary_and_can_trigger_again(self) -> None:
        machine = RollingOISignalStateMachine()
        events = [
            machine.evaluate(rolling_result(value)) for value in (-5.2, -4.0, -3.0, -5.1)
        ]
        self.assertEqual(
            [
                RollingOISignalEventType.TRIGGER,
                None,
                RollingOISignalEventType.REARM,
                RollingOISignalEventType.TRIGGER,
            ],
            [event.event_type if event else None for event in events],
        )

    def test_direct_reversal_emits_opposite_trigger_in_both_directions(self) -> None:
        for first, second, expected in (
            (5.5, -5.2, RollingOISignalDirection.NEGATIVE),
            (-5.5, 5.2, RollingOISignalDirection.POSITIVE),
        ):
            with self.subTest(first=first):
                machine = RollingOISignalStateMachine()
                machine.evaluate(rolling_result(first))
                event = machine.evaluate(rolling_result(second))
                self.assertIsNotNone(event)
                assert event is not None
                self.assertEqual(RollingOISignalEventType.TRIGGER, event.event_type)
                self.assertEqual(expected, event.direction)

    def test_unavailable_result_does_not_rearm_reset_or_update_metadata(self) -> None:
        machine = RollingOISignalStateMachine()
        machine.evaluate(rolling_result(5.5))
        before = machine.state_for("BTCUSDT")

        self.assertIsNone(machine.evaluate(rolling_result(None, available=False)))
        self.assertIsNone(machine.evaluate(rolling_result(None, available=False)))
        self.assertEqual(before, machine.state_for("BTCUSDT"))

    def test_quantity_is_only_trigger_metric_and_context_is_optional(self) -> None:
        machine = RollingOISignalStateMachine()
        event = machine.evaluate(
            rolling_result(5.0, price_change_pct=None, oi_value_change_pct=None)
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertIsNone(event.price_change_pct)
        self.assertIsNone(event.oi_value_change_pct)

        other = RollingOISignalStateMachine()
        self.assertIsNone(
            other.evaluate(
                rolling_result(1.0, price_change_pct=90.0, oi_value_change_pct=80.0)
            )
        )

    def test_state_is_independent_per_symbol_and_prunable(self) -> None:
        machine = RollingOISignalStateMachine()
        machine.evaluate_batch(
            rolling_result(5.1, symbol=symbol) for symbol in ("A", "B", "C")
        )
        self.assertEqual(1, machine.prune(("A", "C")))
        self.assertEqual(RollingOISignalState.NORMAL, machine.state_for("B").state)
        self.assertEqual(
            RollingOISignalState.POSITIVE_TRIGGERED, machine.state_for("A").state
        )
        self.assertEqual(
            RollingOISignalState.POSITIVE_TRIGGERED, machine.state_for("C").state
        )

    def test_event_contains_focused_window_and_transition_context(self) -> None:
        machine = RollingOISignalStateMachine()
        event = machine.evaluate(rolling_result(5.25))
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual("BTCUSDT", event.symbol)
        self.assertEqual(300, event.window_seconds)
        self.assertEqual(NOW, event.latest_observed_at_utc)
        self.assertEqual(NOW - timedelta(minutes=5), event.baseline_observed_at_utc)
        self.assertEqual(300.0, event.actual_window_seconds)
        self.assertEqual(RollingOISignalState.NORMAL, event.previous_state)
        self.assertEqual(RollingOISignalState.POSITIVE_TRIGGERED, event.new_state)

    def test_state_machine_has_no_telegram_or_send_dependency(self) -> None:
        machine = RollingOISignalStateMachine()
        machine.evaluate(rolling_result(5.2))
        names = set(vars(machine)) | set(dir(type(machine)))
        self.assertFalse(any("telegram" in name.lower() for name in names))
        self.assertFalse(any(name.startswith("send") for name in names))
