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


def _require_utc_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be a timezone-aware UTC datetime")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{field_name} must be a timezone-aware UTC datetime")
    return value.astimezone(timezone.utc)


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


@dataclass(frozen=True, slots=True)
class MarkPriceUpdate:
    """One validated Binance mark-price event."""

    symbol: str
    mark_price: float
    exchange_time: datetime
    received_at_utc: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("mark price entry is missing a symbol")
        if isinstance(self.mark_price, bool):
            raise ValueError("mark price must be a finite positive number")
        try:
            mark_price = float(self.mark_price)
        except (TypeError, ValueError) as exc:
            raise ValueError("mark price must be a finite positive number") from exc
        if not math.isfinite(mark_price) or mark_price <= 0:
            raise ValueError("mark price must be a finite positive number")

        object.__setattr__(self, "mark_price", mark_price)
        object.__setattr__(
            self,
            "exchange_time",
            _require_utc_datetime(self.exchange_time, "exchange_time"),
        )
        object.__setattr__(
            self,
            "received_at_utc",
            _require_utc_datetime(self.received_at_utc, "received_at_utc"),
        )

    @classmethod
    def from_binance_payload(
        cls, payload: object, received_at_utc: datetime
    ) -> "MarkPriceUpdate":
        if not isinstance(payload, Mapping):
            raise ValueError("mark price entry must be an object")

        symbol = payload.get("s")
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("mark price entry is missing a symbol")

        if "p" not in payload:
            raise ValueError("mark price entry is missing a price")
        raw_price = payload["p"]
        if isinstance(raw_price, bool):
            raise ValueError("mark price must be a finite positive number")
        try:
            mark_price = float(raw_price)
        except (TypeError, ValueError) as exc:
            raise ValueError("mark price must be a finite positive number") from exc
        if not math.isfinite(mark_price) or mark_price <= 0:
            raise ValueError("mark price must be a finite positive number")

        if "E" not in payload:
            raise ValueError("mark price entry is missing event time")

        return cls(
            symbol=symbol,
            mark_price=mark_price,
            exchange_time=_require_epoch_milliseconds(payload["E"], "event time"),
            received_at_utc=_require_utc_datetime(
                received_at_utc, "received_at_utc"
            ),
        )
