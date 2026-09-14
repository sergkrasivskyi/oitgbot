from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from oitgbot.models import RollingOISample
from oitgbot.services.rolling_oi_store import RollingOIStore


class OIFlashDirection(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


@dataclass(frozen=True, slots=True)
class OIFlashCandidate:
    detected_at_utc: datetime
    symbol: str
    direction: OIFlashDirection
    oi_pct: float
    px_pct: float | None
    current_observed_at_utc: datetime
    baseline_observed_at_utc: datetime
    actual_window_seconds: float
    threshold_pct: float


class OIFlashDetector:
    """Pure rolling OI FLASH detector over the existing in-memory samples."""

    def __init__(
        self,
        *,
        window_seconds: float = 60.0,
        threshold_pct: float = 3.0,
        baseline_max_lag_seconds: float = 45.0,
        price_max_age_seconds: float = 5.0,
    ) -> None:
        for name, value in (
            ("window_seconds", window_seconds),
            ("threshold_pct", threshold_pct),
            ("baseline_max_lag_seconds", baseline_max_lag_seconds),
            ("price_max_age_seconds", price_max_age_seconds),
        ):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        self.window_seconds = float(window_seconds)
        self.threshold_pct = float(threshold_pct)
        self.baseline_max_lag_seconds = float(baseline_max_lag_seconds)
        self.price_max_age_seconds = float(price_max_age_seconds)

    def evaluate(
        self,
        store: RollingOIStore,
        symbols: tuple[str, ...],
        detected_at_utc: datetime,
    ) -> tuple[OIFlashCandidate, ...]:
        detected = _require_utc(detected_at_utc)
        candidates = [
            candidate
            for symbol in dict.fromkeys(symbols)
            if (candidate := self.evaluate_symbol(store, symbol, detected)) is not None
        ]
        candidates.sort(key=lambda item: (-abs(item.oi_pct), item.symbol))
        return tuple(candidates)

    def evaluate_symbol(
        self,
        store: RollingOIStore,
        symbol: str,
        detected_at_utc: datetime,
    ) -> OIFlashCandidate | None:
        history = store.history(symbol)
        if not history:
            return None
        current = history[-1]
        target = current.observed_at_utc - timedelta(seconds=self.window_seconds)
        baseline = next(
            (
                sample
                for sample in reversed(history)
                if sample.observed_at_utc <= target
            ),
            None,
        )
        if baseline is None:
            return None
        baseline_lag = (target - baseline.observed_at_utc).total_seconds()
        if baseline_lag < 0 or baseline_lag > self.baseline_max_lag_seconds:
            return None
        if baseline.oi_quantity <= 0:
            return None
        oi_pct = (current.oi_quantity / baseline.oi_quantity - 1.0) * 100.0
        if not math.isfinite(oi_pct) or abs(oi_pct) < self.threshold_pct:
            return None
        direction = (
            OIFlashDirection.POSITIVE
            if oi_pct >= self.threshold_pct
            else OIFlashDirection.NEGATIVE
        )
        return OIFlashCandidate(
            detected_at_utc=_require_utc(detected_at_utc),
            symbol=symbol,
            direction=direction,
            oi_pct=oi_pct,
            px_pct=self._price_change(current, baseline),
            current_observed_at_utc=current.observed_at_utc,
            baseline_observed_at_utc=baseline.observed_at_utc,
            actual_window_seconds=(
                current.observed_at_utc - baseline.observed_at_utc
            ).total_seconds(),
            threshold_pct=self.threshold_pct,
        )

    def _price_change(
        self, current: RollingOISample, baseline: RollingOISample
    ) -> float | None:
        if not self._valid_price(current) or not self._valid_price(baseline):
            return None
        assert current.mark_price is not None and baseline.mark_price is not None
        if baseline.mark_price <= 0:
            return None
        value = (current.mark_price / baseline.mark_price - 1.0) * 100.0
        return value if math.isfinite(value) else None

    def _valid_price(self, sample: RollingOISample) -> bool:
        if sample.mark_price is None or sample.price_exchange_time is None:
            return False
        if sample.mark_price <= 0 or not math.isfinite(sample.mark_price):
            return False
        age = (sample.observed_at_utc - sample.price_exchange_time).total_seconds()
        return 0 <= age <= self.price_max_age_seconds


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.astimezone(timezone.utc)
