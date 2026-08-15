from __future__ import annotations

import math
import threading
from collections import deque
from datetime import timedelta

from oitgbot.models import RollingOISample


class RollingOIStore:
    """Bounded, chronological current-OI history keyed by symbol."""

    def __init__(
        self,
        *,
        retention_minutes: float = 150.0,
        cadence_seconds: float = 30.0,
        max_samples_per_symbol: int | None = None,
    ) -> None:
        if retention_minutes <= 0 or not math.isfinite(retention_minutes):
            raise ValueError("retention_minutes must be finite and positive")
        if cadence_seconds <= 0 or not math.isfinite(cadence_seconds):
            raise ValueError("cadence_seconds must be finite and positive")

        derived_limit = math.ceil(retention_minutes * 60 / cadence_seconds) + 2
        if max_samples_per_symbol is None:
            max_samples_per_symbol = derived_limit
        if (
            isinstance(max_samples_per_symbol, bool)
            or not isinstance(max_samples_per_symbol, int)
            or max_samples_per_symbol <= 0
        ):
            raise ValueError("max_samples_per_symbol must be a positive integer")

        self.retention_minutes = float(retention_minutes)
        self.cadence_seconds = float(cadence_seconds)
        self.max_samples_per_symbol = max_samples_per_symbol
        self._retention = timedelta(minutes=retention_minutes)
        self._samples: dict[str, deque[RollingOISample]] = {}
        self._lock = threading.RLock()

    def add(self, sample: RollingOISample) -> bool:
        if not isinstance(sample, RollingOISample):
            raise TypeError("sample must be a RollingOISample")

        with self._lock:
            history = self._samples.setdefault(sample.symbol, deque())
            if history:
                latest = history[-1]
                if sample.observed_at_utc < latest.observed_at_utc:
                    return False
                if sample.observed_at_utc == latest.observed_at_utc:
                    if self._prefer_duplicate(sample, latest):
                        history[-1] = sample
                        return True
                    return False

            history.append(sample)
            self._prune_history(history)
            return True

    def latest(self, symbol: str) -> RollingOISample | None:
        with self._lock:
            history = self._samples.get(symbol)
            return history[-1] if history else None

    def history(self, symbol: str) -> tuple[RollingOISample, ...]:
        with self._lock:
            return tuple(self._samples.get(symbol, ()))

    def symbols(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(symbol for symbol, values in self._samples.items() if values))

    def prune(self, symbol: str | None = None) -> int:
        with self._lock:
            targets = (
                [symbol]
                if symbol is not None
                else list(self._samples)
            )
            removed = 0
            for target in targets:
                history = self._samples.get(target)
                if not history:
                    continue
                removed += self._prune_history(history)
                if not history:
                    self._samples.pop(target, None)
            return removed

    def _prune_history(self, history: deque[RollingOISample]) -> int:
        if not history:
            return 0
        removed = 0
        cutoff = history[-1].observed_at_utc - self._retention
        while history and history[0].observed_at_utc < cutoff:
            history.popleft()
            removed += 1
        while len(history) > self.max_samples_per_symbol:
            history.popleft()
            removed += 1
        return removed

    @staticmethod
    def _prefer_duplicate(
        candidate: RollingOISample,
        existing: RollingOISample,
    ) -> bool:
        if candidate.oi_quantity != existing.oi_quantity:
            return False
        if candidate.oi_exchange_time != existing.oi_exchange_time:
            return False

        candidate_context = sum(
            value is not None
            for value in (
                candidate.mark_price,
                candidate.price_exchange_time,
                candidate.oi_value_usd,
            )
        )
        existing_context = sum(
            value is not None
            for value in (
                existing.mark_price,
                existing.price_exchange_time,
                existing.oi_value_usd,
            )
        )
        return candidate_context > existing_context
