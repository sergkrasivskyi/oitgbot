from __future__ import annotations

import argparse
import csv
import gzip
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from oitgbot.services.research_telemetry import ResearchTelemetryStore, require_utc

EXPORT_COLUMNS = ResearchTelemetryStore.COLUMNS


def export_recent(
    db_path: str | Path,
    output_path: str | Path,
    *,
    hours: float,
    symbols: tuple[str, ...] = (),
    now_utc: datetime | None = None,
) -> tuple[int, str | None, str | None]:
    if hours <= 0:
        raise ValueError("hours must be positive")
    now = require_utc(now_utc or datetime.now(timezone.utc), "now_utc")
    cutoff = (now - timedelta(hours=hours)).isoformat()
    store = ResearchTelemetryStore(db_path)
    query = (
        f"SELECT {', '.join(EXPORT_COLUMNS)} FROM research_bars_5m "
        "WHERE is_closed=1 AND bucket_start_utc>=?"
    )
    parameters: list[object] = [cutoff]
    normalized_symbols = tuple(dict.fromkeys(symbol.upper() for symbol in symbols))
    if normalized_symbols:
        placeholders = ", ".join("?" for _ in normalized_symbols)
        query += f" AND symbol IN ({placeholders})"
        parameters.extend(normalized_symbols)
    query += " ORDER BY bucket_start_utc, symbol"

    try:
        with store.connect(read_only=True) as connection:
            rows = list(connection.execute(query, parameters))
    except sqlite3.Error as exc:
        raise RuntimeError(f"research telemetry export failed: {exc}") from exc

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(output, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(EXPORT_COLUMNS)
        writer.writerows(
            tuple(row[column] for column in EXPORT_COLUMNS) for row in rows
        )

    first = rows[0]["bucket_start_utc"] if rows else None
    last = rows[-1]["bucket_start_utc"] if rows else None
    return len(rows), first, last


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export closed 5-minute OI + price research bars to gzip CSV."
    )
    parser.add_argument("--db", type=Path, default=Path("state/oi_research.sqlite3"))
    parser.add_argument("--hours", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--symbol",
        action="append",
        default=[],
        help="Optional symbol filter; repeat for multiple symbols.",
    )
    args = parser.parse_args(argv)
    try:
        count, first, last = export_recent(
            args.db,
            args.output,
            hours=args.hours,
            symbols=tuple(args.symbol),
        )
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(
        f"rows={count} first_bucket={first or 'NA'} last_bucket={last or 'NA'} "
        f"output={args.output.resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
