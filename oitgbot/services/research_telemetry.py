from __future__ import annotations

import asyncio
import logging
import math
import queue
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from oitgbot.models import MarkPriceUpdate, RollingOISample

logger = logging.getLogger("oitgbot.rolling.research")

SCHEMA_VERSION = 1
BUCKET_SECONDS = 300


def require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{field_name} must be a timezone-aware UTC datetime")
    return value.astimezone(timezone.utc)


def bucket_start_utc(value: datetime) -> datetime:
    value = require_utc(value, "timestamp")
    epoch_seconds = int(value.timestamp())
    return datetime.fromtimestamp(
        epoch_seconds - epoch_seconds % BUCKET_SECONDS, tz=timezone.utc
    )


def _iso(value: datetime) -> str:
    return require_utc(value, "timestamp").isoformat()


def _parse(value: str) -> datetime:
    return require_utc(datetime.fromisoformat(value), "stored timestamp")


@dataclass(frozen=True, slots=True)
class ResearchBar:
    symbol: str
    bucket_start_utc: datetime
    oi_open: float | None
    oi_high: float | None
    oi_low: float | None
    oi_close: float | None
    oi_sample_count: int
    first_oi_observed_at_utc: datetime | None
    last_oi_observed_at_utc: datetime | None
    price_open: float | None
    price_high: float | None
    price_low: float | None
    price_close: float | None
    price_sample_count: int
    first_price_event_at_utc: datetime | None
    last_price_event_at_utc: datetime | None
    is_closed: bool


@dataclass(slots=True)
class _MutableBar:
    symbol: str
    bucket_start_utc: datetime
    oi_open: float | None = None
    oi_high: float | None = None
    oi_low: float | None = None
    oi_close: float | None = None
    oi_sample_count: int = 0
    first_oi_observed_at_utc: datetime | None = None
    last_oi_observed_at_utc: datetime | None = None
    price_open: float | None = None
    price_high: float | None = None
    price_low: float | None = None
    price_close: float | None = None
    price_sample_count: int = 0
    first_price_event_at_utc: datetime | None = None
    last_price_event_at_utc: datetime | None = None

    def observe_oi(self, value: float, observed_at_utc: datetime) -> None:
        if self.oi_sample_count == 0:
            self.oi_open = self.oi_high = self.oi_low = self.oi_close = value
            self.first_oi_observed_at_utc = observed_at_utc
        else:
            self.oi_high = max(self.oi_high or value, value)
            self.oi_low = min(self.oi_low or value, value)
            if observed_at_utc < (self.first_oi_observed_at_utc or observed_at_utc):
                self.oi_open = value
                self.first_oi_observed_at_utc = observed_at_utc
            if observed_at_utc >= (self.last_oi_observed_at_utc or observed_at_utc):
                self.oi_close = value
        self.oi_sample_count += 1
        if (
            self.last_oi_observed_at_utc is None
            or observed_at_utc >= self.last_oi_observed_at_utc
        ):
            self.last_oi_observed_at_utc = observed_at_utc

    def observe_price(self, value: float, event_at_utc: datetime) -> None:
        if self.price_sample_count == 0:
            self.price_open = self.price_high = self.price_low = self.price_close = (
                value
            )
            self.first_price_event_at_utc = event_at_utc
        else:
            self.price_high = max(self.price_high or value, value)
            self.price_low = min(self.price_low or value, value)
            if event_at_utc < (self.first_price_event_at_utc or event_at_utc):
                self.price_open = value
                self.first_price_event_at_utc = event_at_utc
            if event_at_utc >= (self.last_price_event_at_utc or event_at_utc):
                self.price_close = value
        self.price_sample_count += 1
        if (
            self.last_price_event_at_utc is None
            or event_at_utc >= self.last_price_event_at_utc
        ):
            self.last_price_event_at_utc = event_at_utc

    def freeze(self, *, closed: bool) -> ResearchBar:
        return ResearchBar(
            symbol=self.symbol,
            bucket_start_utc=self.bucket_start_utc,
            oi_open=self.oi_open,
            oi_high=self.oi_high,
            oi_low=self.oi_low,
            oi_close=self.oi_close,
            oi_sample_count=self.oi_sample_count,
            first_oi_observed_at_utc=self.first_oi_observed_at_utc,
            last_oi_observed_at_utc=self.last_oi_observed_at_utc,
            price_open=self.price_open,
            price_high=self.price_high,
            price_low=self.price_low,
            price_close=self.price_close,
            price_sample_count=self.price_sample_count,
            first_price_event_at_utc=self.first_price_event_at_utc,
            last_price_event_at_utc=self.last_price_event_at_utc,
            is_closed=closed,
        )


class ResearchTelemetryStore:
    """Versioned SQLite storage for durable 5-minute research bars."""

    COLUMNS = (
        "symbol",
        "bucket_start_utc",
        "oi_open",
        "oi_high",
        "oi_low",
        "oi_close",
        "oi_sample_count",
        "first_oi_observed_at_utc",
        "last_oi_observed_at_utc",
        "price_open",
        "price_high",
        "price_low",
        "price_close",
        "price_sample_count",
        "first_price_event_at_utc",
        "last_price_event_at_utc",
        "is_closed",
    )

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms

    @contextmanager
    def connect(self, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
        if read_only:
            connection = sqlite3.connect(
                f"file:{self.path.resolve().as_posix()}?mode=ro",
                uri=True,
                timeout=self.busy_timeout_ms / 1000,
            )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        if not read_only:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
        try:
            yield connection
            if not read_only:
                connection.commit()
        except Exception:
            if not read_only:
                connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_bars_5m (
                    symbol TEXT NOT NULL,
                    bucket_start_utc TEXT NOT NULL,
                    oi_open REAL,
                    oi_high REAL,
                    oi_low REAL,
                    oi_close REAL,
                    oi_sample_count INTEGER NOT NULL DEFAULT 0,
                    first_oi_observed_at_utc TEXT,
                    last_oi_observed_at_utc TEXT,
                    price_open REAL,
                    price_high REAL,
                    price_low REAL,
                    price_close REAL,
                    price_sample_count INTEGER NOT NULL DEFAULT 0,
                    first_price_event_at_utc TEXT,
                    last_price_event_at_utc TEXT,
                    is_closed INTEGER NOT NULL DEFAULT 0,
                    updated_at_utc TEXT NOT NULL,
                    PRIMARY KEY (symbol, bucket_start_utc)
                );
                CREATE INDEX IF NOT EXISTS idx_research_bars_5m_bucket
                ON research_bars_5m(bucket_start_utc);
                """
            )
            connection.execute(
                "INSERT INTO schema_metadata(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def write_bars(self, bars: tuple[ResearchBar, ...]) -> int:
        if not bars:
            return 0
        with self.connect() as connection:
            for bar in bars:
                existing = connection.execute(
                    "SELECT * FROM research_bars_5m WHERE symbol=? AND bucket_start_utc=?",
                    (bar.symbol, _iso(bar.bucket_start_utc)),
                ).fetchone()
                merged = self._merge(existing, bar) if existing else bar
                values = self._values(merged)
                placeholders = ", ".join("?" for _ in values)
                columns = ", ".join((*self.COLUMNS, "updated_at_utc"))
                updates = ", ".join(
                    f"{column}=excluded.{column}" for column in self.COLUMNS[2:]
                )
                connection.execute(
                    f"INSERT INTO research_bars_5m ({columns}) VALUES ({placeholders}) "
                    f"ON CONFLICT(symbol, bucket_start_utc) DO UPDATE SET {updates}, "
                    "updated_at_utc=excluded.updated_at_utc",
                    values,
                )
        return len(bars)

    @staticmethod
    def _merge(existing: sqlite3.Row, incoming: ResearchBar) -> ResearchBar:
        def dt(name: str) -> datetime | None:
            return _parse(existing[name]) if existing[name] else None

        old_oi_first = dt("first_oi_observed_at_utc")
        old_oi_last = dt("last_oi_observed_at_utc")
        old_price_first = dt("first_price_event_at_utc")
        old_price_last = dt("last_price_event_at_utc")
        oi_first_is_old = old_oi_first is not None and (
            incoming.first_oi_observed_at_utc is None
            or old_oi_first <= incoming.first_oi_observed_at_utc
        )
        oi_last_is_old = old_oi_last is not None and (
            incoming.last_oi_observed_at_utc is None
            or old_oi_last >= incoming.last_oi_observed_at_utc
        )
        price_first_is_old = old_price_first is not None and (
            incoming.first_price_event_at_utc is None
            or old_price_first <= incoming.first_price_event_at_utc
        )
        price_last_is_old = old_price_last is not None and (
            incoming.last_price_event_at_utc is None
            or old_price_last >= incoming.last_price_event_at_utc
        )

        def extremum(
            old: float | None, new: float | None, function: Any
        ) -> float | None:
            values = [value for value in (old, new) if value is not None]
            return function(values) if values else None

        return ResearchBar(
            symbol=incoming.symbol,
            bucket_start_utc=incoming.bucket_start_utc,
            oi_open=existing["oi_open"] if oi_first_is_old else incoming.oi_open,
            oi_high=extremum(existing["oi_high"], incoming.oi_high, max),
            oi_low=extremum(existing["oi_low"], incoming.oi_low, min),
            oi_close=existing["oi_close"] if oi_last_is_old else incoming.oi_close,
            oi_sample_count=existing["oi_sample_count"] + incoming.oi_sample_count,
            first_oi_observed_at_utc=(
                old_oi_first if oi_first_is_old else incoming.first_oi_observed_at_utc
            ),
            last_oi_observed_at_utc=(
                old_oi_last if oi_last_is_old else incoming.last_oi_observed_at_utc
            ),
            price_open=(
                existing["price_open"] if price_first_is_old else incoming.price_open
            ),
            price_high=extremum(existing["price_high"], incoming.price_high, max),
            price_low=extremum(existing["price_low"], incoming.price_low, min),
            price_close=(
                existing["price_close"] if price_last_is_old else incoming.price_close
            ),
            price_sample_count=existing["price_sample_count"]
            + incoming.price_sample_count,
            first_price_event_at_utc=(
                old_price_first
                if price_first_is_old
                else incoming.first_price_event_at_utc
            ),
            last_price_event_at_utc=(
                old_price_last
                if price_last_is_old
                else incoming.last_price_event_at_utc
            ),
            is_closed=bool(existing["is_closed"]) or incoming.is_closed,
        )

    @staticmethod
    def _values(bar: ResearchBar) -> tuple[Any, ...]:
        optional_times = (
            bar.first_oi_observed_at_utc,
            bar.last_oi_observed_at_utc,
            bar.first_price_event_at_utc,
            bar.last_price_event_at_utc,
        )
        return (
            bar.symbol,
            _iso(bar.bucket_start_utc),
            bar.oi_open,
            bar.oi_high,
            bar.oi_low,
            bar.oi_close,
            bar.oi_sample_count,
            _iso(optional_times[0]) if optional_times[0] else None,
            _iso(optional_times[1]) if optional_times[1] else None,
            bar.price_open,
            bar.price_high,
            bar.price_low,
            bar.price_close,
            bar.price_sample_count,
            _iso(optional_times[2]) if optional_times[2] else None,
            _iso(optional_times[3]) if optional_times[3] else None,
            int(bar.is_closed),
            _iso(datetime.now(timezone.utc)),
        )

    def prune(self, cutoff_utc: datetime) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM research_bars_5m WHERE bucket_start_utc < ?",
                (_iso(cutoff_utc),),
            )
            return max(cursor.rowcount, 0)

    def rows(self, *, closed_only: bool = True) -> list[sqlite3.Row]:
        query = "SELECT * FROM research_bars_5m"
        if closed_only:
            query += " WHERE is_closed=1"
        query += " ORDER BY bucket_start_utc, symbol"
        with self.connect(read_only=True) as connection:
            return list(connection.execute(query))


class ResearchTelemetry:
    """Aggregate hot-path observations in memory and persist them in batches."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        retention_days: float = 14.0,
        clock: Any = lambda: datetime.now(timezone.utc),
    ) -> None:
        if retention_days <= 0 or not math.isfinite(retention_days):
            raise ValueError("retention_days must be finite and positive")
        self.store = ResearchTelemetryStore(db_path)
        self.retention_days = float(retention_days)
        self._clock = clock
        self._lock = threading.RLock()
        self._eligible_symbols: set[str] = set()
        self._bars: dict[tuple[str, datetime], _MutableBar] = {}
        self._closure_watermark: datetime | None = None
        self._commands: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._writer_thread: threading.Thread | None = None
        self._running = False
        self._last_prune_utc: datetime | None = None
        self.rows_written = 0
        self.write_failures = 0
        self.last_completed_bucket: datetime | None = None

    def start(self) -> None:
        if self._running:
            return
        self.store.initialize()
        self._running = True
        self._writer_thread = threading.Thread(
            target=self._writer_loop, name="oi-research-writer", daemon=True
        )
        self._writer_thread.start()
        logger.info(
            "RESEARCH_TELEMETRY status=started db_path=%s retention_days=%.1f schema=%d",
            self.store.path,
            self.retention_days,
            SCHEMA_VERSION,
        )

    def set_eligible_symbols(self, symbols: Any) -> None:
        with self._lock:
            self._eligible_symbols = {str(symbol).upper() for symbol in symbols}

    def observe_oi(self, sample: RollingOISample) -> None:
        self._observe(
            sample.symbol,
            sample.observed_at_utc,
            oi_value=sample.oi_quantity,
        )

    def observe_price(self, update: MarkPriceUpdate) -> None:
        self._observe(
            update.symbol,
            update.exchange_time,
            price_value=update.mark_price,
        )

    def _observe(
        self,
        symbol: str,
        timestamp: datetime,
        *,
        oi_value: float | None = None,
        price_value: float | None = None,
    ) -> None:
        if not self._running:
            return
        timestamp = require_utc(timestamp, "observation timestamp")
        symbol = symbol.upper()
        with self._lock:
            if symbol not in self._eligible_symbols:
                return
            current_bucket = bucket_start_utc(timestamp)
            closed: tuple[ResearchBar, ...] = ()
            if (
                self._closure_watermark is None
                or current_bucket > self._closure_watermark
            ):
                closed = self._close_before_locked(current_bucket)
                self._closure_watermark = current_bucket
            bar = self._bars.setdefault(
                (symbol, current_bucket), _MutableBar(symbol, current_bucket)
            )
            if oi_value is not None:
                bar.observe_oi(oi_value, timestamp)
            if price_value is not None:
                bar.observe_price(price_value, timestamp)
            if current_bucket < self._closure_watermark:
                closed += (
                    self._bars.pop((symbol, current_bucket)).freeze(closed=True),
                )
            if closed:
                newest_closed = max(bar.bucket_start_utc for bar in closed)
                if (
                    self.last_completed_bucket is None
                    or newest_closed > self.last_completed_bucket
                ):
                    self.last_completed_bucket = newest_closed
        if closed:
            self._commands.put(("write", closed))

    def _close_before_locked(self, cutoff: datetime) -> tuple[ResearchBar, ...]:
        keys = [key for key in self._bars if key[1] < cutoff]
        if not keys:
            return ()
        bars = tuple(self._bars.pop(key).freeze(closed=True) for key in sorted(keys))
        return bars

    async def stop(self) -> None:
        await asyncio.to_thread(self.stop_sync)

    def stop_sync(self) -> None:
        if not self._running:
            return
        with self._lock:
            partial = tuple(bar.freeze(closed=False) for bar in self._bars.values())
            self._bars.clear()
        if partial:
            self._commands.put(("write", partial))
        self._commands.put(("stop", None))
        if self._writer_thread is not None:
            self._writer_thread.join()
        self._running = False
        logger.info(
            "RESEARCH_TELEMETRY status=stopped rows_written=%d write_failures=%d "
            "last_completed_bucket=%s",
            self.rows_written,
            self.write_failures,
            self.last_completed_bucket.isoformat()
            if self.last_completed_bucket
            else "NA",
        )

    def _writer_loop(self) -> None:
        while True:
            command, payload = self._commands.get()
            if command == "stop":
                return
            try:
                bars = tuple(payload)
                written = self.store.write_bars(bars)
                self.rows_written += written
                closed_buckets = [bar.bucket_start_utc for bar in bars if bar.is_closed]
                logger.info(
                    "RESEARCH_TELEMETRY status=flushed bars_written=%d "
                    "last_completed_bucket=%s",
                    written,
                    max(closed_buckets).isoformat() if closed_buckets else "NA",
                )
                reference = max(
                    (bar.bucket_start_utc for bar in bars), default=self._clock()
                )
                if (
                    self._last_prune_utc is None
                    or reference - self._last_prune_utc >= timedelta(hours=1)
                ):
                    removed = self.store.prune(
                        reference - timedelta(days=self.retention_days)
                    )
                    self._last_prune_utc = reference
                    logger.info(
                        "RESEARCH_TELEMETRY status=pruned rows_removed=%d cutoff_utc=%s",
                        removed,
                        (reference - timedelta(days=self.retention_days)).isoformat(),
                    )
            except Exception:
                self.write_failures += 1
                logger.exception(
                    "RESEARCH_TELEMETRY status=write_failed failures=%d",
                    self.write_failures,
                )
