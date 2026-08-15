from __future__ import annotations

import math
import threading
from collections.abc import Iterable
from datetime import datetime, timezone

from oitgbot.models import MarkPriceUpdate


def _validate_reference_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("reference time must be a timezone-aware UTC datetime")
    return value.astimezone(timezone.utc)


class PriceStateStore:
    """Thread-safe latest mark-price state, optionally filtered by symbol."""

    def __init__(self, eligible_symbols: Iterable[str] | None = None) -> None:
        self._eligible_symbols = (
            frozenset(eligible_symbols) if eligible_symbols is not None else None
        )
        self._prices: dict[str, MarkPriceUpdate] = {}
        self._lock = threading.RLock()

    def update(self, update: MarkPriceUpdate) -> bool:
        if not isinstance(update, MarkPriceUpdate):
            raise TypeError("update must be a MarkPriceUpdate")

        with self._lock:
            if (
                self._eligible_symbols is not None
                and update.symbol not in self._eligible_symbols
            ):
                return False

            current = self._prices.get(update.symbol)
            if current is not None and update.exchange_time <= current.exchange_time:
                return False

            self._prices[update.symbol] = update
            return True

    def get(self, symbol: str) -> MarkPriceUpdate | None:
        with self._lock:
            return self._prices.get(symbol)

    def get_fresh(
        self,
        symbol: str,
        now_utc: datetime,
        max_age_seconds: float,
    ) -> MarkPriceUpdate | None:
        reference = _validate_reference_time(now_utc)
        if (
            isinstance(max_age_seconds, bool)
            or not isinstance(max_age_seconds, (int, float))
            or not math.isfinite(max_age_seconds)
            or max_age_seconds < 0
        ):
            raise ValueError("max_age_seconds must be a finite non-negative number")

        with self._lock:
            update = self._prices.get(symbol)
            if update is None:
                return None
            age_seconds = (reference - update.received_at_utc).total_seconds()
            if age_seconds < 0 or age_seconds > max_age_seconds:
                return None
            return update

    def snapshot(self) -> dict[str, MarkPriceUpdate]:
        with self._lock:
            return dict(self._prices)

    def set_eligible_symbols(self, symbols: Iterable[str]) -> None:
        """Atomically align the accepted stream universe and discard outsiders."""
        eligible = {symbol.upper() for symbol in symbols}
        with self._lock:
            self._eligible_symbols = eligible
            self._prices = {
                symbol: update
                for symbol, update in self._prices.items()
                if symbol in eligible
            }
