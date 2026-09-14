from __future__ import annotations

import argparse
import csv
import gzip
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from oitgbot.services.oi_flash_research import from_epoch_ms, to_epoch_ms

TIMESTAMP_COLUMNS = {
    "minute_start_utc",
    "oi_observed_at_utc",
    "price_observed_at_utc",
    "detected_at_utc",
    "current_observed_at_utc",
    "baseline_observed_at_utc",
}


def export_flash_research(
    db_path: str | Path,
    output: str | Path,
    *,
    hours: float = 168.0,
    now_utc: datetime | None = None,
) -> tuple[Path, int, Path, int]:
    if hours <= 0:
        raise ValueError("hours must be positive")
    source = Path(db_path)
    now = now_utc or datetime.now(timezone.utc)
    cutoff = to_epoch_ms(now - timedelta(hours=hours))
    base = _output_base(Path(output))
    bars_path = base.with_name(base.name + "-bars.csv.gz")
    events_path = base.with_name(base.name + "-events.csv.gz")
    connection = sqlite3.connect(
        f"file:{source.resolve().as_posix()}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        bars = connection.execute(
            "SELECT * FROM flash_bars_1m WHERE minute_start_utc>=? "
            "ORDER BY minute_start_utc, symbol",
            (cutoff,),
        ).fetchall()
        events = connection.execute(
            "SELECT * FROM flash_events WHERE detected_at_utc>=? "
            "ORDER BY detected_at_utc, symbol, direction",
            (cutoff,),
        ).fetchall()
    finally:
        connection.close()
    _write_rows(bars_path, bars)
    _write_rows(events_path, events)
    return bars_path, len(bars), events_path, len(events)


def _output_base(path: Path) -> Path:
    text = str(path)
    for suffix in (".csv.gz", ".gz", ".csv"):
        if text.lower().endswith(suffix):
            return Path(text[: -len(suffix)])
    return path


def _write_rows(path: Path, rows: list[sqlite3.Row]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(rows[0].keys()) if rows else []
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        if columns:
            writer.writeheader()
            for row in rows:
                rendered = dict(row)
                for column in TIMESTAMP_COLUMNS.intersection(rendered):
                    if rendered[column] is not None:
                        rendered[column] = from_epoch_ms(rendered[column]).isoformat()
                writer.writerow(rendered)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export FLASH bars and events")
    parser.add_argument("--db", default="state/oi_flash_research.sqlite3")
    parser.add_argument("--hours", type=float, default=168.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    bars, bar_count, events, event_count = export_flash_research(
        args.db, args.output, hours=args.hours
    )
    print(f"bars={bar_count} output={bars}")
    print(f"events={event_count} output={events}")


if __name__ == "__main__":
    main()
