from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone


def _require_positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer")

    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isdigit():
        parsed = int(value)
    else:
        raise ValueError(f"{field_name} must be a positive integer")

    if parsed <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return parsed


def _require_epoch_milliseconds(value: object, field_name: str) -> datetime:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an epoch timestamp in milliseconds")

    if isinstance(value, int):
        timestamp_ms = value
    elif isinstance(value, str) and value.isdigit():
        timestamp_ms = int(value)
    else:
        raise ValueError(f"{field_name} must be an epoch timestamp in milliseconds")

    if timestamp_ms < 0:
        raise ValueError(f"{field_name} must be an epoch timestamp in milliseconds")

    try:
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(
            f"{field_name} must be an epoch timestamp in milliseconds"
        ) from exc


@dataclass(slots=True)
class OIRow:
    symbol: str
    oi_pct: float
    price_pct: float = 0.0


@dataclass(frozen=True, slots=True)
class CurrentOpenInterest:
    """A current open-interest quantity reported by Binance."""

    symbol: str
    oi_quantity: float
    exchange_time: datetime

    @classmethod
    def from_binance_payload(
        cls, payload: object, requested_symbol: str
    ) -> "CurrentOpenInterest":
        if not isinstance(payload, Mapping):
            raise ValueError("current open interest response must be an object")

        response_symbol = payload.get("symbol")
        if not isinstance(response_symbol, str) or not response_symbol:
            raise ValueError("current open interest response is missing a symbol")
        if response_symbol != requested_symbol:
            raise ValueError(
                "current open interest response symbol does not match the request"
            )

        if "openInterest" not in payload:
            raise ValueError("current open interest response is missing openInterest")
        open_interest = payload["openInterest"]
        if isinstance(open_interest, bool):
            raise ValueError("openInterest must be a finite non-negative number")
        try:
            oi_quantity = float(open_interest)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "openInterest must be a finite non-negative number"
            ) from exc
        if not math.isfinite(oi_quantity) or oi_quantity < 0:
            raise ValueError("openInterest must be a finite non-negative number")

        if "time" not in payload:
            raise ValueError("current open interest response is missing time")
        exchange_time = _require_epoch_milliseconds(payload["time"], "time")

        return cls(
            symbol=response_symbol,
            oi_quantity=oi_quantity,
            exchange_time=exchange_time,
        )


@dataclass(frozen=True, slots=True)
class BinanceRateLimit:
    """A rate-limit rule returned by Binance exchangeInfo."""

    rate_limit_type: str
    interval: str
    interval_num: int
    limit: int

    @classmethod
    def from_binance_payload(cls, payload: object) -> "BinanceRateLimit":
        if not isinstance(payload, Mapping):
            raise ValueError("rate limit entry must be an object")

        rate_limit_type = payload.get("rateLimitType")
        interval = payload.get("interval")
        if not isinstance(rate_limit_type, str) or not rate_limit_type:
            raise ValueError("rateLimitType must be a non-empty string")
        if not isinstance(interval, str) or not interval:
            raise ValueError("interval must be a non-empty string")

        return cls(
            rate_limit_type=rate_limit_type,
            interval=interval,
            interval_num=_require_positive_int(payload.get("intervalNum"), "intervalNum"),
            limit=_require_positive_int(payload.get("limit"), "limit"),
        )
