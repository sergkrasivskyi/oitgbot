"""Offline 15m anomaly research CLI. Never imports or starts the application."""

from __future__ import annotations

import argparse
import csv
import sqlite3
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path

from oitgbot.services.oi_anomaly_15m import AnomalyResult, Observation, utc
from oitgbot.services.oi_anomaly_15m_reader import analyze_database

CSV_COLUMNS = tuple(field.name for field in fields(Observation)) + tuple(
    field.name for field in fields(AnomalyResult) if field.name != "observation"
)


def export_csv(output: Path, results: tuple[AnomalyResult, ...]) -> None:
    # Exclusive creation prevents accidental overwriting of a DB, WAL or other file.
    with output.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for result in results:
            row = asdict(result)
            row.update(row.pop("observation"))
            writer.writerow(
                {
                    key: value.isoformat() if isinstance(value, datetime) else value
                    for key, value in row.items()
                }
            )


def number(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.4f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only aligned 15m OI anomaly research"
    )
    parser.add_argument("--db", type=Path, default=Path("state/oi_research.sqlite3"))
    parser.add_argument(
        "--as-of", help="UTC timestamp; analyze the interval closed by this time"
    )
    parser.add_argument("--symbol", action="append", default=[])
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument(
        "--output", type=Path, help="New CSV file; exports all results, not just --top"
    )
    parser.add_argument("--baseline-days", type=int, default=14)
    parser.add_argument("--min-history", type=int, default=96)
    args = parser.parse_args(argv)
    try:
        if args.top < 1:
            raise ValueError("--top must be positive")
        as_of = (
            utc(datetime.fromisoformat(args.as_of.replace("Z", "+00:00")))
            if args.as_of
            else None
        )
        start, results = analyze_database(
            args.db,
            as_of=as_of,
            symbols=tuple(args.symbol),
            baseline_days=args.baseline_days,
            min_history=args.min_history,
        )
        if args.output:
            export_csv(args.output, results)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(
        f"Research interval start: {start.isoformat() if start else 'N/A (no complete interval)'}"
    )
    print("SYMBOL Z15 OI15% PX15% ROBUST_Z PERCENTILE HISTORY_N STATUS")
    for result in results[: args.top]:
        obs = result.observation
        print(
            obs.symbol,
            number(result.z_score),
            number(obs.oi_change_pct),
            number(obs.price_change_pct),
            number(result.robust_z_score),
            number(result.percentile),
            result.baseline_count,
            result.z_unavailable_reason or "ok",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
