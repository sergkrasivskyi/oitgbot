from __future__ import annotations

import argparse
import sqlite3
import statistics
from dataclasses import dataclass
from pathlib import Path

from oitgbot.services.research_telemetry import ResearchTelemetryStore


@dataclass(frozen=True, slots=True)
class SampleCountStats:
    minimum: int | None
    median: float | None
    maximum: int | None


@dataclass(frozen=True, slots=True)
class LatestBucketStatus:
    bucket_start_utc: str
    total_bars: int
    bars_with_oi: int
    bars_with_price: int
    bars_with_both: int
    oi_samples: SampleCountStats
    price_samples: SampleCountStats


@dataclass(frozen=True, slots=True)
class TelemetryStatus:
    db_path: Path
    db_size_bytes: int
    schema_version: str | None
    closed_bars: int
    closed_symbols: int
    first_closed_bucket_utc: str | None
    last_closed_bucket_utc: str | None
    partial_bars: int
    closed_bars_with_oi: int
    oi_samples: SampleCountStats
    oi_without_price: int
    closed_bars_with_price: int
    price_samples: SampleCountStats
    price_without_oi: int
    duplicate_symbol_bucket_groups: int
    invalid_bucket_count: int
    null_or_empty_symbol_count: int
    latest_bucket: LatestBucketStatus | None


def _sample_stats(values: list[int]) -> SampleCountStats:
    if not values:
        return SampleCountStats(None, None, None)
    return SampleCountStats(min(values), statistics.median(values), max(values))


def inspect_telemetry(db_path: str | Path) -> TelemetryStatus:
    """Read one research database without altering it or contacting any service."""
    path = Path(db_path).resolve()
    if not path.is_file():
        raise RuntimeError(f"research telemetry database does not exist: {path}")
    store = ResearchTelemetryStore(path)
    try:
        with store.connect(read_only=True) as connection:
            version_row = connection.execute(
                "SELECT value FROM schema_metadata WHERE key='schema_version'"
            ).fetchone()
            summary = connection.execute(
                """
                SELECT
                    COUNT(*) AS closed_bars,
                    COUNT(DISTINCT symbol) AS closed_symbols,
                    MIN(bucket_start_utc) AS first_bucket,
                    MAX(bucket_start_utc) AS last_bucket,
                    SUM(oi_sample_count > 0) AS oi_bars,
                    SUM(price_sample_count > 0) AS price_bars,
                    SUM(oi_sample_count > 0 AND price_sample_count = 0) AS oi_only,
                    SUM(price_sample_count > 0 AND oi_sample_count = 0) AS price_only
                FROM research_bars_5m
                WHERE is_closed=1
                """
            ).fetchone()
            partial_bars = connection.execute(
                "SELECT COUNT(*) FROM research_bars_5m WHERE is_closed=0"
            ).fetchone()[0]
            duplicate_groups = connection.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT symbol, bucket_start_utc
                    FROM research_bars_5m
                    GROUP BY symbol, bucket_start_utc
                    HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
            invalid_buckets = connection.execute(
                """
                SELECT COUNT(*) FROM research_bars_5m
                WHERE strftime('%s', bucket_start_utc) IS NULL
                   OR CAST(strftime('%s', bucket_start_utc) AS INTEGER) % 300 != 0
                """
            ).fetchone()[0]
            invalid_symbols = connection.execute(
                "SELECT COUNT(*) FROM research_bars_5m WHERE symbol IS NULL OR trim(symbol)=''"
            ).fetchone()[0]
            oi_samples = [
                row[0]
                for row in connection.execute(
                    "SELECT oi_sample_count FROM research_bars_5m "
                    "WHERE is_closed=1 AND oi_sample_count > 0"
                )
            ]
            price_samples = [
                row[0]
                for row in connection.execute(
                    "SELECT price_sample_count FROM research_bars_5m "
                    "WHERE is_closed=1 AND price_sample_count > 0"
                )
            ]
            latest_bucket = _latest_bucket_status(connection, summary["last_bucket"])
    except Exception as exc:
        raise RuntimeError(f"research telemetry status failed: {exc}") from exc

    return TelemetryStatus(
        db_path=path,
        db_size_bytes=path.stat().st_size,
        schema_version=version_row[0] if version_row else None,
        closed_bars=summary["closed_bars"],
        closed_symbols=summary["closed_symbols"],
        first_closed_bucket_utc=summary["first_bucket"],
        last_closed_bucket_utc=summary["last_bucket"],
        partial_bars=partial_bars,
        closed_bars_with_oi=summary["oi_bars"] or 0,
        oi_samples=_sample_stats(oi_samples),
        oi_without_price=summary["oi_only"] or 0,
        closed_bars_with_price=summary["price_bars"] or 0,
        price_samples=_sample_stats(price_samples),
        price_without_oi=summary["price_only"] or 0,
        duplicate_symbol_bucket_groups=duplicate_groups,
        invalid_bucket_count=invalid_buckets,
        null_or_empty_symbol_count=invalid_symbols,
        latest_bucket=latest_bucket,
    )


def _latest_bucket_status(
    connection: sqlite3.Connection, latest: str | None
) -> LatestBucketStatus | None:
    if latest is None:
        return None
    rows = list(
        connection.execute(
            """
            SELECT oi_sample_count, price_sample_count
            FROM research_bars_5m
            WHERE is_closed=1 AND bucket_start_utc=?
            """,
            (latest,),
        )
    )
    oi_samples = [row["oi_sample_count"] for row in rows if row["oi_sample_count"] > 0]
    price_samples = [
        row["price_sample_count"] for row in rows if row["price_sample_count"] > 0
    ]
    return LatestBucketStatus(
        bucket_start_utc=latest,
        total_bars=len(rows),
        bars_with_oi=len(oi_samples),
        bars_with_price=len(price_samples),
        bars_with_both=sum(
            row["oi_sample_count"] > 0 and row["price_sample_count"] > 0 for row in rows
        ),
        oi_samples=_sample_stats(oi_samples),
        price_samples=_sample_stats(price_samples),
    )


def _format_stats(value: SampleCountStats) -> str:
    if value.minimum is None:
        return "NA / NA / NA"
    return f"{value.minimum} / {value.median:g} / {value.maximum}"


def format_status(status: TelemetryStatus) -> str:
    lines = [
        "Research telemetry status",
        f"DB path: {status.db_path}",
        f"DB size bytes: {status.db_size_bytes}",
        f"Schema version: {status.schema_version or 'NA'}",
        "",
        "Closed telemetry",
        f"Closed 5m bars: {status.closed_bars}",
        f"Distinct symbols: {status.closed_symbols}",
        f"First closed bucket UTC: {status.first_closed_bucket_utc or 'NA'}",
        f"Last closed bucket UTC: {status.last_closed_bucket_utc or 'NA'}",
        "",
        "Partial telemetry",
        f"Partial bars: {status.partial_bars}",
        "",
        "OI quality",
        f"Closed bars with OI: {status.closed_bars_with_oi}",
        f"OI sample count min / median / max: {_format_stats(status.oi_samples)}",
        f"Closed OI bars without price: {status.oi_without_price}",
        "",
        "Price quality",
        f"Closed bars with price: {status.closed_bars_with_price}",
        f"Price sample count min / median / max: {_format_stats(status.price_samples)}",
        f"Closed price bars without OI: {status.price_without_oi}",
        "",
        "Integrity",
        f"Duplicate symbol+bucket groups: {status.duplicate_symbol_bucket_groups}",
        f"Invalid/non-5m bucket rows: {status.invalid_bucket_count}",
        f"NULL/empty symbol rows: {status.null_or_empty_symbol_count}",
    ]
    latest = status.latest_bucket
    if latest is None:
        lines.extend(("", "Latest closed bucket", "Unavailable"))
    else:
        lines.extend(
            (
                "",
                "Latest closed bucket",
                f"Bucket start UTC: {latest.bucket_start_utc}",
                f"Total symbols/bars: {latest.total_bars}",
                f"Bars with OI: {latest.bars_with_oi}",
                f"Bars with price: {latest.bars_with_price}",
                f"Bars with both OI and price: {latest.bars_with_both}",
                f"OI sample count min / median / max: {_format_stats(latest.oi_samples)}",
                (
                    "Price sample count min / median / max: "
                    f"{_format_stats(latest.price_samples)}"
                ),
            )
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only health summary for Task 21 research telemetry."
    )
    parser.add_argument("--db", type=Path, default=Path("state/oi_research.sqlite3"))
    args = parser.parse_args(argv)
    try:
        print(format_status(inspect_telemetry(args.db)))
    except RuntimeError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
