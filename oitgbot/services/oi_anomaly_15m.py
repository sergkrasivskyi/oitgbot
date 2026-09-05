"""Pure aligned OI research analytics; no runtime or I/O dependencies."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

INTERVAL = timedelta(minutes=15)
BAR = timedelta(minutes=5)
EPSILON = 1e-12  # Absolute percentage points, shared by sigma and MAD.
MAD_SCALE = 0.6744897501960817


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def align_15m(value: datetime) -> datetime:
    value = utc(value)
    return value.replace(minute=value.minute // 15 * 15, second=0, microsecond=0)


@dataclass(frozen=True, slots=True)
class SourceBar:
    symbol: str
    bucket_start_utc: datetime
    oi_open: float | None
    oi_close: float | None
    price_open: float | None
    price_close: float | None
    oi_sample_count: int
    price_sample_count: int
    is_closed: bool


@dataclass(frozen=True, slots=True)
class Observation:
    symbol: str
    interval_start_utc: datetime
    interval_end_utc: datetime
    oi_start: float | None
    oi_end: float | None
    oi_change_pct: float | None
    price_start: float | None
    price_end: float | None
    price_change_pct: float | None
    constituent_5m_bar_count: int
    total_oi_sample_count: int
    min_oi_sample_count: int
    total_price_sample_count: int
    min_price_sample_count: int
    source_coverage: float
    is_valid: bool
    invalid_reason: str | None


def positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def aggregate_interval(
    symbol: str, start: datetime, bars: Iterable[SourceBar]
) -> Observation:
    start = utc(start)
    if align_15m(start) != start:
        raise ValueError("interval start must be aligned to 15m")
    rows = sorted(bars, key=lambda row: row.bucket_start_utc)
    expected = [start + i * BAR for i in range(3)]
    identities = [utc(row.bucket_start_utc) for row in rows]
    reason = None
    if identities != expected or any(row.symbol != symbol for row in rows):
        reason = "missing_or_invalid_constituents"
    elif not all(row.is_closed for row in rows):
        reason = "open_source_bar"
    first = next((row for row in rows if row.bucket_start_utc == start), None)
    last = next((row for row in rows if row.bucket_start_utc == expected[-1]), None)
    endpoints = (
        first.oi_open if first else None,
        last.oi_close if last else None,
        first.price_open if first else None,
        last.price_close if last else None,
    )
    oi_start, oi_end, price_start, price_end = endpoints
    oi_pct = price_pct = None
    if reason is None:
        if not all(positive(value) for value in endpoints):
            reason = "invalid_endpoint"
        else:
            oi_pct = (oi_end - oi_start) / oi_start * 100
            price_pct = (price_end - price_start) / price_start * 100
            if not all(math.isfinite(value) for value in (oi_pct, price_pct)):
                reason = "non_finite_return"
                oi_pct = price_pct = None
    coverage = (
        len(
            {
                row.bucket_start_utc
                for row in rows
                if row.symbol == symbol
                and row.is_closed
                and row.bucket_start_utc in expected
            }
        )
        / 3
    )
    return Observation(
        symbol,
        start,
        start + INTERVAL,
        *endpoints[:2],
        oi_pct,
        *endpoints[2:],
        price_pct,
        len(rows),
        sum(row.oi_sample_count for row in rows),
        min((row.oi_sample_count for row in rows), default=0),
        sum(row.price_sample_count for row in rows),
        min((row.price_sample_count for row in rows), default=0),
        coverage,
        reason is None,
        reason,
    )


def aggregate_bars(bars: Iterable[SourceBar]) -> tuple[Observation, ...]:
    groups = defaultdict(list)
    for bar in bars:
        groups[bar.symbol, align_15m(bar.bucket_start_utc)].append(bar)
    return tuple(
        aggregate_interval(symbol, start, rows)
        for (symbol, start), rows in sorted(groups.items())
    )


@dataclass(frozen=True, slots=True)
class AnomalyResult:
    observation: Observation
    z_score: float | None
    robust_z_score: float | None
    percentile: float | None
    baseline_count: int
    requested_baseline_days: int
    baseline_coverage: float
    oldest_baseline_interval_start_utc: datetime | None
    newest_baseline_interval_start_utc: datetime | None
    baseline_mean_oi_pct: float | None
    baseline_population_std_oi_pct: float | None
    baseline_median_oi_pct: float | None
    baseline_mad_oi_pct: float | None
    z_unavailable_reason: str | None
    robust_z_unavailable_reason: str | None


def validate_options(baseline_days: int, min_history: int) -> None:
    if baseline_days < 1 or min_history < 1:
        raise ValueError("baseline_days and min_history must be positive integers")


def score_observation(
    current: Observation,
    history: Iterable[Observation],
    *,
    baseline_days: int = 14,
    min_history: int = 96,
) -> AnomalyResult:
    validate_options(baseline_days, min_history)
    cutoff = current.interval_start_utc - timedelta(days=baseline_days)
    prior = {}
    for item in history:
        if (
            item.symbol == current.symbol
            and item.is_valid
            and cutoff <= item.interval_start_utc < current.interval_start_utc
            and item.interval_end_utc <= current.interval_start_utc
            and item.oi_change_pct is not None
            and math.isfinite(item.oi_change_pct)
        ):
            if item.interval_start_utc in prior:
                raise ValueError("duplicate baseline interval")
            prior[item.interval_start_utc] = item.oi_change_pct
    values = [prior[start] for start in sorted(prior)]
    n = len(values)
    mean = sigma = median = mad = None
    statistics_invalid = False
    if values:
        try:
            mean = statistics.mean(values)
            sigma = statistics.pstdev(values)
            median = statistics.median(values)
            mad = statistics.median(abs(value - median) for value in values)
            statistics_invalid = not all(
                math.isfinite(value) for value in (mean, sigma, median, mad)
            )
        except (OverflowError, ValueError):
            statistics_invalid = True
        if statistics_invalid:
            mean = sigma = median = mad = None
    z = robust = percentile = None
    x = current.oi_change_pct
    reason = None
    if not current.is_valid or x is None or not math.isfinite(x):
        reason = current.invalid_reason or "invalid_current_observation"
    elif n < min_history:
        reason = "insufficient_history"
    robust_reason = reason
    if reason is None and statistics_invalid:
        reason = robust_reason = "non_finite_baseline_statistics"
    if current.is_valid and x is not None and math.isfinite(x) and n:
        percentile = (
            100 * (sum(v < x for v in values) + 0.5 * sum(v == x for v in values)) / n
        )
    if reason is None:
        if sigma <= EPSILON:
            reason = "zero_or_near_zero_std"
        else:
            z = (x - mean) / sigma
            if not math.isfinite(z):
                z, reason = None, "non_finite_z"
        if mad <= EPSILON:
            robust_reason = "zero_or_near_zero_mad"
        else:
            robust = MAD_SCALE * (x - median) / mad
            if not math.isfinite(robust):
                robust, robust_reason = None, "non_finite_robust_z"
    return AnomalyResult(
        current,
        z,
        robust,
        percentile,
        n,
        baseline_days,
        n / (baseline_days * 96),
        min(prior, default=None),
        max(prior, default=None),
        mean,
        sigma,
        median,
        mad,
        reason,
        robust_reason,
    )


def research_sort_key(result: AnomalyResult) -> tuple:
    """Presentation only; no production eligibility filtering."""
    z, oi = result.z_score, result.observation.oi_change_pct
    return (
        z is None,
        -z if z is not None else 0,
        -(oi if oi is not None and math.isfinite(oi) else -math.inf),
        result.observation.symbol,
    )
