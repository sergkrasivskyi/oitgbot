from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from oitgbot.models import RollingOIWindowResult


class RollingOISignalState(str, Enum):
    NORMAL = "NORMAL"
    POSITIVE_TRIGGERED = "POSITIVE_TRIGGERED"
    NEGATIVE_TRIGGERED = "NEGATIVE_TRIGGERED"


class RollingOISignalDirection(str, Enum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"


class RollingOISignalEventType(str, Enum):
    TRIGGER = "TRIGGER"
    REARM = "REARM"


@dataclass(frozen=True, slots=True)
class RollingOISignalEvent:
    symbol: str
    window_seconds: int
    direction: RollingOISignalDirection
    oi_quantity_change_pct: float
    trigger_threshold_pct: float
    rearm_threshold_pct: float
    latest_observed_at_utc: datetime | None
    baseline_observed_at_utc: datetime | None
    actual_window_seconds: float | None
    price_change_pct: float | None
    oi_value_change_pct: float | None
    previous_state: RollingOISignalState
    new_state: RollingOISignalState
    event_type: RollingOISignalEventType


@dataclass(slots=True)
class RollingOISymbolSignalState:
    state: RollingOISignalState = RollingOISignalState.NORMAL
    triggered_at_utc: datetime | None = None
    trigger_value_pct: float | None = None
    last_evaluated_at_utc: datetime | None = None
    last_value_pct: float | None = None


class RollingOISignalStateMachine:
    """In-memory per-symbol hysteresis for the rolling 5m quantity impulse."""

    WINDOW_SECONDS = 300

    def __init__(
        self,
        *,
        trigger_threshold_pct: float = 5.0,
        rearm_threshold_pct: float = 3.0,
    ) -> None:
        if isinstance(trigger_threshold_pct, bool) or isinstance(
            rearm_threshold_pct, bool
        ):
            raise ValueError("signal thresholds must be numbers, not booleans")
        try:
            trigger = float(trigger_threshold_pct)
            rearm = float(rearm_threshold_pct)
        except (TypeError, ValueError) as exc:
            raise ValueError("signal thresholds must be finite numbers") from exc
        if not math.isfinite(trigger) or trigger <= 0:
            raise ValueError("trigger_threshold_pct must be finite and > 0")
        if not math.isfinite(rearm) or rearm < 0:
            raise ValueError("rearm_threshold_pct must be finite and >= 0")
        if rearm >= trigger:
            raise ValueError(
                "rearm_threshold_pct must be less than trigger_threshold_pct"
            )
        self.trigger_threshold_pct = trigger
        self.rearm_threshold_pct = rearm
        self._states: dict[str, RollingOISymbolSignalState] = {}

    def evaluate(
        self, result: RollingOIWindowResult
    ) -> RollingOISignalEvent | None:
        if result.window_seconds != self.WINDOW_SECONDS:
            raise ValueError("rolling OI signal state machine accepts only 5m results")
        if not result.available or result.oi_quantity_change_pct is None:
            return None

        value = result.oi_quantity_change_pct
        if not math.isfinite(value):
            return None

        symbol_state = self._states.setdefault(
            result.symbol, RollingOISymbolSignalState()
        )
        previous = symbol_state.state
        event_type: RollingOISignalEventType | None = None
        direction: RollingOISignalDirection | None = None
        new_state = previous

        if previous is RollingOISignalState.NORMAL:
            if value >= self.trigger_threshold_pct:
                event_type = RollingOISignalEventType.TRIGGER
                direction = RollingOISignalDirection.POSITIVE
                new_state = RollingOISignalState.POSITIVE_TRIGGERED
            elif value <= -self.trigger_threshold_pct:
                event_type = RollingOISignalEventType.TRIGGER
                direction = RollingOISignalDirection.NEGATIVE
                new_state = RollingOISignalState.NEGATIVE_TRIGGERED
        elif previous is RollingOISignalState.POSITIVE_TRIGGERED:
            if value <= -self.trigger_threshold_pct:
                event_type = RollingOISignalEventType.TRIGGER
                direction = RollingOISignalDirection.NEGATIVE
                new_state = RollingOISignalState.NEGATIVE_TRIGGERED
            elif value <= self.rearm_threshold_pct:
                event_type = RollingOISignalEventType.REARM
                direction = RollingOISignalDirection.POSITIVE
                new_state = RollingOISignalState.NORMAL
        elif previous is RollingOISignalState.NEGATIVE_TRIGGERED:
            if value >= self.trigger_threshold_pct:
                event_type = RollingOISignalEventType.TRIGGER
                direction = RollingOISignalDirection.POSITIVE
                new_state = RollingOISignalState.POSITIVE_TRIGGERED
            elif value >= -self.rearm_threshold_pct:
                event_type = RollingOISignalEventType.REARM
                direction = RollingOISignalDirection.NEGATIVE
                new_state = RollingOISignalState.NORMAL

        symbol_state.last_evaluated_at_utc = result.latest_timestamp
        symbol_state.last_value_pct = value
        if event_type is RollingOISignalEventType.TRIGGER:
            symbol_state.triggered_at_utc = result.latest_timestamp
            symbol_state.trigger_value_pct = value
        elif event_type is RollingOISignalEventType.REARM:
            symbol_state.triggered_at_utc = None
            symbol_state.trigger_value_pct = None
        symbol_state.state = new_state

        if event_type is None or direction is None:
            return None
        return RollingOISignalEvent(
            symbol=result.symbol,
            window_seconds=result.window_seconds,
            direction=direction,
            oi_quantity_change_pct=value,
            trigger_threshold_pct=self.trigger_threshold_pct,
            rearm_threshold_pct=self.rearm_threshold_pct,
            latest_observed_at_utc=result.latest_timestamp,
            baseline_observed_at_utc=result.baseline_timestamp,
            actual_window_seconds=result.actual_window_seconds,
            price_change_pct=result.price_change_pct,
            oi_value_change_pct=result.oi_value_change_pct,
            previous_state=previous,
            new_state=new_state,
            event_type=event_type,
        )

    def evaluate_batch(
        self, results: Iterable[RollingOIWindowResult]
    ) -> tuple[RollingOISignalEvent, ...]:
        events = []
        for result in results:
            event = self.evaluate(result)
            if event is not None:
                events.append(event)
        return tuple(events)

    def state_for(self, symbol: str) -> RollingOISymbolSignalState:
        state = self._states.get(symbol)
        if state is None:
            return RollingOISymbolSignalState()
        return RollingOISymbolSignalState(
            state=state.state,
            triggered_at_utc=state.triggered_at_utc,
            trigger_value_pct=state.trigger_value_pct,
            last_evaluated_at_utc=state.last_evaluated_at_utc,
            last_value_pct=state.last_value_pct,
        )

    def prune(self, eligible_symbols: Iterable[str]) -> int:
        eligible = set(eligible_symbols)
        removed = set(self._states).difference(eligible)
        for symbol in removed:
            del self._states[symbol]
        return len(removed)

    def active_counts(self) -> tuple[int, int]:
        positive = sum(
            state.state is RollingOISignalState.POSITIVE_TRIGGERED
            for state in self._states.values()
        )
        negative = sum(
            state.state is RollingOISignalState.NEGATIVE_TRIGGERED
            for state in self._states.values()
        )
        return positive, negative
