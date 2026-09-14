from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from oitgbot.services.oi_flash_research import from_epoch_ms, to_epoch_ms


def collect_status(path: str | Path, *, now_utc: datetime | None = None) -> dict:
    db_path = Path(path)
    now = now_utc or datetime.now(timezone.utc)
    status = {"path": str(db_path), "exists": db_path.exists()}
    if not db_path.exists():
        return status
    connection = sqlite3.connect(
        f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        bars = connection.execute(
            "SELECT COUNT(*) AS count, COUNT(DISTINCT symbol) AS symbols, "
            "MIN(minute_start_utc) AS oldest, MAX(minute_start_utc) AS newest "
            "FROM flash_bars_1m"
        ).fetchone()
        events = connection.execute(
            "SELECT COUNT(*) AS count, "
            "SUM(suppressed_by_cooldown=0) AS accepted, "
            "SUM(suppressed_by_cooldown=1) AS suppressed, "
            "SUM(telegram_sent=1) AS telegram_sent, "
            "SUM(telegram_attempted=1 AND telegram_sent=0) AS telegram_failed "
            "FROM flash_events"
        ).fetchone()
        recent_cutoff = to_epoch_ms(now - timedelta(minutes=5))
        recent = connection.execute(
            "SELECT COUNT(*) AS count, COUNT(DISTINCT symbol) AS symbols "
            "FROM flash_bars_1m WHERE minute_start_utc>=?",
            (recent_cutoff,),
        ).fetchone()
        version = connection.execute(
            "SELECT value FROM schema_metadata WHERE key='schema_version'"
        ).fetchone()
        status.update(
            integrity=integrity,
            schema_version=version[0] if version else None,
            bars=bars["count"],
            symbols=bars["symbols"],
            oldest_bar_utc=_iso(bars["oldest"]),
            newest_bar_utc=_iso(bars["newest"]),
            recent_5m_bars=recent["count"],
            recent_5m_symbols=recent["symbols"],
            events=events["count"],
            accepted=events["accepted"] or 0,
            cooldown_suppressed=events["suppressed"] or 0,
            telegram_sent=events["telegram_sent"] or 0,
            telegram_failed=events["telegram_failed"] or 0,
            size_bytes=db_path.stat().st_size,
        )
        return status
    finally:
        connection.close()


def _iso(value: int | None) -> str | None:
    return from_epoch_ms(value).isoformat() if value is not None else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect the FLASH research DB")
    parser.add_argument("--db", default="state/oi_flash_research.sqlite3")
    args = parser.parse_args()
    for key, value in collect_status(args.db).items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
