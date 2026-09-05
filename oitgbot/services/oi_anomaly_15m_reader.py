"""Read-only, WAL-aware SQLite adapter for offline anomaly research."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path

from .oi_anomaly_15m import (
    INTERVAL,
    AnomalyResult,
    SourceBar,
    aggregate_bars,
    aggregate_interval,
    align_15m,
    research_sort_key,
    score_observation,
    utc,
    validate_options,
)

SOURCE_COLUMNS = (
    "symbol",
    "bucket_start_utc",
    "oi_open",
    "oi_close",
    "price_open",
    "price_close",
    "oi_sample_count",
    "price_sample_count",
    "is_closed",
)


@contextmanager
def connect_read_only(path: str | Path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")  # One coherent WAL snapshot across reads.
        yield connection
    finally:
        connection.close()


def read_bars(connection, start, end, symbols=(), *, descending=False):
    query = (
        f"SELECT {', '.join(SOURCE_COLUMNS)} FROM research_bars_5m "
        "WHERE bucket_start_utc>=? AND bucket_start_utc<?"
    )
    parameters = [start.isoformat(), end.isoformat()]
    if symbols:
        query += " AND symbol IN (" + ",".join("?" for _ in symbols) + ")"
        parameters.extend(symbols)
    query += (
        " ORDER BY bucket_start_utc DESC, symbol"
        if descending
        else " ORDER BY symbol, bucket_start_utc"
    )
    for row in connection.execute(query, parameters):
        values = dict(row)
        values["bucket_start_utc"] = utc(
            datetime.fromisoformat(values["bucket_start_utc"])
        )
        yield SourceBar(**values)


def latest_complete_start(connection, symbols, now):
    """Stream newest interval groups; stop at the first valid observation."""
    end = align_15m(now)
    rows = read_bars(
        connection,
        datetime(1970, 1, 1, tzinfo=timezone.utc),
        end,
        symbols,
        descending=True,
    )
    try:
        for start, group in groupby(
            rows, key=lambda bar: align_15m(bar.bucket_start_utc)
        ):
            if any(item.is_valid for item in aggregate_bars(group)):
                return start
    finally:
        rows.close()
    return None


def analyze_database(
    path: str | Path,
    *,
    as_of: datetime | None = None,
    symbols: tuple[str, ...] = (),
    baseline_days: int = 14,
    min_history: int = 96,
    now_utc: datetime | None = None,
) -> tuple[datetime | None, tuple[AnomalyResult, ...]]:
    start, results, _ = analyze_database_with_observations(
        path,
        as_of=as_of,
        symbols=symbols,
        baseline_days=baseline_days,
        min_history=min_history,
        now_utc=now_utc,
    )
    return start, results


def analyze_database_with_observations(
    path: str | Path,
    *,
    as_of: datetime | None = None,
    symbols: tuple[str, ...] = (),
    baseline_days: int = 14,
    min_history: int = 96,
    now_utc: datetime | None = None,
) -> tuple[datetime | None, tuple[AnomalyResult, ...], tuple]:
    """Return Task 23 results with their shared bounded aligned history."""
    validate_options(baseline_days, min_history)
    symbols = tuple(sorted({symbol.upper() for symbol in symbols}))
    with connect_read_only(path) as connection:
        start = (
            align_15m(as_of) - INTERVAL
            if as_of is not None
            else latest_complete_start(
                connection, symbols, utc(now_utc or datetime.now(timezone.utc))
            )
        )
        if start is None:
            return None, (), ()
        results = []
        seen = set()
        all_observations = []
        rows = read_bars(
            connection, start - timedelta(days=baseline_days), start + INTERVAL, symbols
        )
        for symbol, group in groupby(rows, key=lambda bar: bar.symbol):
            observations = aggregate_bars(group)
            all_observations.extend(observations)
            current = next(
                (item for item in observations if item.interval_start_utc == start),
                aggregate_interval(symbol, start, ()),
            )
            results.append(
                score_observation(
                    current,
                    observations,
                    baseline_days=baseline_days,
                    min_history=min_history,
                )
            )
            seen.add(symbol)
        for symbol in set(symbols) - seen:
            results.append(
                score_observation(
                    aggregate_interval(symbol, start, ()),
                    (),
                    baseline_days=baseline_days,
                    min_history=min_history,
                )
            )
    return start, tuple(sorted(results, key=research_sort_key)), tuple(all_observations)


@dataclass(frozen=True, slots=True)
class IntervalSnapshot:
    interval_start_utc: datetime
    source_rows: tuple[SourceBar, ...]
    observations: tuple
    is_complete: bool


def read_interval_snapshot(
    path: str | Path,
    interval_start_utc: datetime,
    *,
    symbols: tuple[str, ...] = (),
) -> IntervalSnapshot:
    """Read one coherent WAL snapshot for incremental live processing."""
    start = utc(interval_start_utc)
    if align_15m(start) != start:
        raise ValueError("interval start must be aligned to 15m")
    symbols = tuple(sorted({symbol.upper() for symbol in symbols}))
    with connect_read_only(path) as connection:
        bars = tuple(read_bars(connection, start, start + INTERVAL, symbols))
    grouped = {symbol: [] for symbol in symbols}
    for bar in bars:
        grouped.setdefault(bar.symbol, []).append(bar)
    observations = tuple(
        aggregate_interval(symbol, start, grouped[symbol]) for symbol in sorted(grouped)
    )
    last_bucket = start + timedelta(minutes=10)
    complete = any(
        bar.bucket_start_utc == last_bucket and bar.is_closed for bar in bars
    )
    return IntervalSnapshot(start, bars, observations, complete)
