from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Sequence

from oitgbot.models import (
    LongAccumulationMetrics,
    RollingOISample,
    RollingOIWindowResult,
)
from oitgbot.services.rolling_oi_store import RollingOIStore

WINDOW_5M_SECONDS = 300
WINDOW_20M_SECONDS = 1_200
WINDOW_60M_SECONDS = 3_600
WINDOW_120M_SECONDS = 7_200
SUPPORTED_WINDOW_SECONDS = frozenset(
    {
        WINDOW_5M_SECONDS,
        WINDOW_20M_SECONDS,
        WINDOW_60M_SECONDS,
        WINDOW_120M_SECONDS,
    }
)


def _change_pct(current: float, baseline: float) -> float | None:
    if baseline <= 0:
        return None
    return (current - baseline) / baseline * 100.0


def _select_at_or_before(
    history: Sequence[RollingOISample],
    target: datetime,
    tolerance_seconds: float,
) -> RollingOISample | None:
    for sample in reversed(history):
        if sample.oi_exchange_time <= target:
            offset = (target - sample.oi_exchange_time).total_seconds()
            return sample if offset <= tolerance_seconds else None
    return None


class RollingOICalculator:
    """Calculate deterministic rolling windows from exchange-time history."""

    def __init__(
        self,
        *,
        cadence_seconds: float = 30.0,
        tolerance_seconds: float | None = None,
    ) -> None:
        if cadence_seconds <= 0 or not math.isfinite(cadence_seconds):
            raise ValueError("cadence_seconds must be finite and positive")
        if tolerance_seconds is None:
            tolerance_seconds = max(60.0, 2 * cadence_seconds)
        if tolerance_seconds < 0 or not math.isfinite(tolerance_seconds):
            raise ValueError("tolerance_seconds must be finite and non-negative")
        self.cadence_seconds = float(cadence_seconds)
        self.tolerance_seconds = float(tolerance_seconds)

    def calculate(
        self,
        store: RollingOIStore,
        symbol: str,
        window_seconds: int,
    ) -> RollingOIWindowResult:
        if (
            isinstance(window_seconds, bool)
            or not isinstance(window_seconds, int)
            or window_seconds <= 0
        ):
            raise ValueError("window_seconds must be a positive integer")

        history = store.history(symbol)
        if not history:
            return self._unavailable(symbol, window_seconds, "no samples")

        latest = history[-1]
        target = latest.oi_exchange_time - timedelta(seconds=window_seconds)
        baseline = _select_at_or_before(
            history, target, self.tolerance_seconds
        )
        if baseline is None:
            before_target = any(
                sample.oi_exchange_time <= target for sample in history
            )
            reason = (
                "baseline outside tolerance"
                if before_target
                else "no baseline at or before target"
            )
            return self._unavailable(
                symbol,
                window_seconds,
                reason,
                latest=latest,
                target=target,
            )

        offset = (target - baseline.oi_exchange_time).total_seconds()
        actual_window = (
            latest.oi_exchange_time - baseline.oi_exchange_time
        ).total_seconds()
        if baseline.oi_quantity == 0:
            return self._unavailable(
                symbol,
                window_seconds,
                "baseline OI quantity is zero",
                latest=latest,
                baseline=baseline,
                target=target,
                actual_window_seconds=actual_window,
                baseline_offset_seconds=offset,
            )

        price_change = None
        if latest.mark_price is not None and baseline.mark_price is not None:
            price_change = _change_pct(latest.mark_price, baseline.mark_price)

        value_change = None
        if (
            latest.oi_value_usd is not None
            and baseline.oi_value_usd is not None
        ):
            value_change = _change_pct(
                latest.oi_value_usd, baseline.oi_value_usd
            )

        return RollingOIWindowResult(
            symbol=symbol,
            window_seconds=window_seconds,
            available=True,
            unavailable_reason=None,
            latest_timestamp=latest.oi_exchange_time,
            baseline_timestamp=baseline.oi_exchange_time,
            target_timestamp=target,
            actual_window_seconds=actual_window,
            baseline_offset_seconds=offset,
            latest_oi_quantity=latest.oi_quantity,
            baseline_oi_quantity=baseline.oi_quantity,
            oi_quantity_change_pct=_change_pct(
                latest.oi_quantity, baseline.oi_quantity
            ),
            latest_mark_price=latest.mark_price,
            baseline_mark_price=baseline.mark_price,
            price_change_pct=price_change,
            latest_oi_value_usd=latest.oi_value_usd,
            baseline_oi_value_usd=baseline.oi_value_usd,
            oi_value_change_pct=value_change,
        )

    def calculate_5m(
        self, store: RollingOIStore, symbol: str
    ) -> RollingOIWindowResult:
        return self.calculate(store, symbol, WINDOW_5M_SECONDS)

    def calculate_20m(
        self, store: RollingOIStore, symbol: str
    ) -> RollingOIWindowResult:
        return self.calculate(store, symbol, WINDOW_20M_SECONDS)

    def calculate_60m(
        self, store: RollingOIStore, symbol: str
    ) -> RollingOIWindowResult:
        return self.calculate(store, symbol, WINDOW_60M_SECONDS)

    def calculate_120m(
        self, store: RollingOIStore, symbol: str
    ) -> RollingOIWindowResult:
        return self.calculate(store, symbol, WINDOW_120M_SECONDS)

    @staticmethod
    def _unavailable(
        symbol: str,
        window_seconds: int,
        reason: str,
        *,
        latest: RollingOISample | None = None,
        baseline: RollingOISample | None = None,
        target: datetime | None = None,
        actual_window_seconds: float | None = None,
        baseline_offset_seconds: float | None = None,
    ) -> RollingOIWindowResult:
        return RollingOIWindowResult(
            symbol=symbol,
            window_seconds=window_seconds,
            available=False,
            unavailable_reason=reason,
            latest_timestamp=latest.oi_exchange_time if latest else None,
            baseline_timestamp=baseline.oi_exchange_time if baseline else None,
            target_timestamp=target,
            actual_window_seconds=actual_window_seconds,
            baseline_offset_seconds=baseline_offset_seconds,
            latest_oi_quantity=latest.oi_quantity if latest else None,
            baseline_oi_quantity=baseline.oi_quantity if baseline else None,
            oi_quantity_change_pct=None,
            latest_mark_price=latest.mark_price if latest else None,
            baseline_mark_price=baseline.mark_price if baseline else None,
            price_change_pct=None,
            latest_oi_value_usd=latest.oi_value_usd if latest else None,
            baseline_oi_value_usd=baseline.oi_value_usd if baseline else None,
            oi_value_change_pct=None,
        )


class AccumulationAnalyzer:
    """Describe 60m/120m accumulation shape without applying thresholds."""

    def __init__(self, calculator: RollingOICalculator) -> None:
        self.calculator = calculator

    def analyze(
        self,
        store: RollingOIStore,
        symbol: str,
        window_seconds: int,
    ) -> LongAccumulationMetrics:
        if window_seconds not in (WINDOW_60M_SECONDS, WINDOW_120M_SECONDS):
            raise ValueError("long accumulation window must be 60m or 120m")

        expected_blocks = window_seconds // 600
        window = self.calculator.calculate(store, symbol, window_seconds)
        if not window.available:
            return LongAccumulationMetrics(
                symbol=symbol,
                window_seconds=window_seconds,
                available=False,
                unavailable_reason=window.unavailable_reason,
                net_oi_change_pct=None,
                persistence=None,
                positive_blocks=0,
                negative_blocks=0,
                flat_blocks=0,
                valid_blocks=0,
                expected_blocks=expected_blocks,
                trend_efficiency=None,
                trend_direction=None,
                max_drawdown_pct=None,
                impulse_concentration=None,
                max_5m_change_pct=None,
                coverage_ratio=0.0,
            )

        history = store.history(symbol)
        latest_time = history[-1].oi_exchange_time
        ten_minute_anchors = self._anchors(
            history, latest_time, window_seconds, 600
        )
        directional_deltas = [
            end.oi_quantity - start.oi_quantity
            for start, end in zip(ten_minute_anchors, ten_minute_anchors[1:])
            if start is not None and end is not None
        ]
        positive_blocks = sum(delta > 0 for delta in directional_deltas)
        negative_blocks = sum(delta < 0 for delta in directional_deltas)
        flat_blocks = sum(delta == 0 for delta in directional_deltas)
        valid_blocks = len(directional_deltas)
        persistence = (
            positive_blocks / valid_blocks if valid_blocks else None
        )

        valid_path = [
            anchor.oi_quantity
            for anchor in ten_minute_anchors
            if anchor is not None
        ]
        trend_efficiency, trend_direction = self._trend(valid_path)
        max_drawdown = self._max_drawdown_pct(valid_path)

        five_minute_anchors = self._anchors(
            history, latest_time, window_seconds, 300
        )
        five_minute_pairs = [
            (start.oi_quantity, end.oi_quantity)
            for start, end in zip(five_minute_anchors, five_minute_anchors[1:])
            if start is not None and end is not None
        ]
        positive_deltas = [max(end - start, 0.0) for start, end in five_minute_pairs]
        total_positive_delta = sum(positive_deltas)
        impulse_concentration = (
            min(1.0, max(positive_deltas) / total_positive_delta)
            if total_positive_delta > 0
            else None
        )
        five_minute_changes = [
            change
            for start, end in five_minute_pairs
            if (change := _change_pct(end, start)) is not None
        ]

        return LongAccumulationMetrics(
            symbol=symbol,
            window_seconds=window_seconds,
            available=True,
            unavailable_reason=None,
            net_oi_change_pct=window.oi_quantity_change_pct,
            persistence=persistence,
            positive_blocks=positive_blocks,
            negative_blocks=negative_blocks,
            flat_blocks=flat_blocks,
            valid_blocks=valid_blocks,
            expected_blocks=expected_blocks,
            trend_efficiency=trend_efficiency,
            trend_direction=trend_direction,
            max_drawdown_pct=max_drawdown,
            impulse_concentration=impulse_concentration,
            max_5m_change_pct=max(five_minute_changes, default=None),
            coverage_ratio=valid_blocks / expected_blocks,
        )

    def analyze_60m(
        self, store: RollingOIStore, symbol: str
    ) -> LongAccumulationMetrics:
        return self.analyze(store, symbol, WINDOW_60M_SECONDS)

    def analyze_120m(
        self, store: RollingOIStore, symbol: str
    ) -> LongAccumulationMetrics:
        return self.analyze(store, symbol, WINDOW_120M_SECONDS)

    def _anchors(
        self,
        history: Sequence[RollingOISample],
        latest_time: datetime,
        window_seconds: int,
        block_seconds: int,
    ) -> list[RollingOISample | None]:
        start = latest_time - timedelta(seconds=window_seconds)
        return [
            _select_at_or_before(
                history,
                start + timedelta(seconds=offset),
                self.calculator.tolerance_seconds,
            )
            for offset in range(0, window_seconds + 1, block_seconds)
        ]

    @staticmethod
    def _trend(quantities: Sequence[float]) -> tuple[float | None, str | None]:
        if len(quantities) < 2:
            return None, None
        net_change = quantities[-1] - quantities[0]
        path_movement = sum(
            abs(current - previous)
            for previous, current in zip(quantities, quantities[1:])
        )
        direction = "positive" if net_change > 0 else "negative" if net_change < 0 else "flat"
        efficiency = (
            min(1.0, abs(net_change) / path_movement)
            if path_movement
            else 0.0
        )
        return efficiency, direction

    @staticmethod
    def _max_drawdown_pct(quantities: Sequence[float]) -> float | None:
        if not quantities:
            return None
        running_peak = quantities[0]
        max_drawdown = 0.0
        for quantity in quantities[1:]:
            running_peak = max(running_peak, quantity)
            if running_peak > 0:
                drawdown = (running_peak - quantity) / running_peak * 100.0
                max_drawdown = max(max_drawdown, drawdown)
        return max_drawdown
