from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone


UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_iso(value: datetime | None) -> str:
    if value is None:
        return "invalid"
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_binance_timestamp(value: object) -> tuple[datetime | None, str | None]:
    """Parse Binance epoch timestamps without inventing a value for bad input."""
    if value is None:
        return None, "missing"
    if isinstance(value, bool):
        return None, "malformed"
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None, "malformed"
    if not math.isfinite(seconds):
        return None, "malformed"
    if abs(seconds) >= 10_000_000_000:
        seconds /= 1000.0
    try:
        return datetime.fromtimestamp(seconds, UTC), None
    except (OverflowError, OSError, ValueError):
        return None, "malformed"


@dataclass(frozen=True, slots=True)
class OIWindowDiagnostic:
    symbol: str
    window: str
    scan_utc: datetime
    start_utc: datetime | None
    end_utc: datetime | None
    start_oi: float
    end_oi: float
    change_pct: float
    expected_gap_seconds: int
    actual_gap_seconds: float | None
    latest_age_seconds: float | None
    start_timestamp_issue: str | None
    end_timestamp_issue: str | None

    @property
    def has_invalid_timestamp(self) -> bool:
        return self.start_timestamp_issue is not None or self.end_timestamp_issue is not None

    @property
    def has_gap_mismatch(self) -> bool:
        return self.actual_gap_seconds is not None and self.actual_gap_seconds != self.expected_gap_seconds

    def anomaly_codes(self) -> list[str]:
        codes: list[str] = []
        if self.start_timestamp_issue is not None:
            codes.append(f"start_timestamp_{self.start_timestamp_issue}")
        if self.end_timestamp_issue is not None:
            codes.append(f"end_timestamp_{self.end_timestamp_issue}")
        if self.start_utc is not None and self.end_utc is not None:
            if self.end_utc < self.start_utc:
                codes.append("end_before_start")
            if self.has_gap_mismatch:
                codes.append("gap_mismatch")
        if self.end_utc is not None and self.end_utc > self.scan_utc:
            codes.append("latest_timestamp_future")
        return codes


def build_oi_window_diagnostic(
    *,
    symbol: str,
    window: str,
    start_timestamp: object,
    end_timestamp: object,
    start_oi: float,
    end_oi: float,
    change_pct: float,
    expected_gap_seconds: int,
    scan_utc: datetime,
) -> OIWindowDiagnostic:
    if scan_utc.tzinfo is None:
        raise ValueError("scan_utc must be timezone-aware")
    scan_utc = scan_utc.astimezone(UTC)
    start_utc, start_issue = parse_binance_timestamp(start_timestamp)
    end_utc, end_issue = parse_binance_timestamp(end_timestamp)
    actual_gap_seconds = (
        (end_utc - start_utc).total_seconds()
        if start_utc is not None and end_utc is not None
        else None
    )
    latest_age_seconds = (scan_utc - end_utc).total_seconds() if end_utc is not None else None
    return OIWindowDiagnostic(
        symbol=symbol,
        window=window,
        scan_utc=scan_utc,
        start_utc=start_utc,
        end_utc=end_utc,
        start_oi=start_oi,
        end_oi=end_oi,
        change_pct=change_pct,
        expected_gap_seconds=expected_gap_seconds,
        actual_gap_seconds=actual_gap_seconds,
        latest_age_seconds=latest_age_seconds,
        start_timestamp_issue=start_issue,
        end_timestamp_issue=end_issue,
    )
