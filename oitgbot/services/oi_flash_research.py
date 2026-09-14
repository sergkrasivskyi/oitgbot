from __future__ import annotations

import asyncio
import logging
import math
import queue
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from oitgbot.models import MarkPriceUpdate, RollingOISample
from oitgbot.services.oi_flash import OIFlashCandidate, OIFlashDirection

logger = logging.getLogger("oitgbot.rolling.oi_flash.research")

SCHEMA_VERSION = 1
MINUTE_MS = 60_000


def to_epoch_ms(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("timestamp must be timezone-aware UTC")
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def from_epoch_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def minute_start_ms(value: datetime) -> int:
    timestamp = to_epoch_ms(value)
    return timestamp - timestamp % MINUTE_MS


@dataclass(frozen=True, slots=True)
class FlashBar1m:
    symbol: str
    minute_start_ms: int
    oi_close: float | None
    oi_observed_at_ms: int | None
    price_close: float | None
    price_observed_at_ms: int | None
    oi_observation_count: int
    price_observation_count: int


@dataclass(slots=True)
class _MutableFlashBar:
    symbol: str
    minute_start_ms: int
    oi_close: float | None = None
    oi_observed_at_ms: int | None = None
    price_close: float | None = None
    price_observed_at_ms: int | None = None
    oi_observation_count: int = 0
    price_observation_count: int = 0

    def observe_oi(self, value: float, observed_at_ms: int) -> None:
        self.oi_observation_count += 1
        if self.oi_observed_at_ms is None or observed_at_ms >= self.oi_observed_at_ms:
            self.oi_close = value
            self.oi_observed_at_ms = observed_at_ms

    def observe_price(self, value: float, observed_at_ms: int) -> None:
        self.price_observation_count += 1
        if (
            self.price_observed_at_ms is None
            or observed_at_ms >= self.price_observed_at_ms
        ):
            self.price_close = value
            self.price_observed_at_ms = observed_at_ms

    def freeze(self) -> FlashBar1m:
        return FlashBar1m(
            self.symbol,
            self.minute_start_ms,
            self.oi_close,
            self.oi_observed_at_ms,
            self.price_close,
            self.price_observed_at_ms,
            self.oi_observation_count,
            self.price_observation_count,
        )


@dataclass(frozen=True, slots=True)
class PersistedFlashEvent:
    event_id: int
    candidate: OIFlashCandidate
    suppressed_by_cooldown: bool


class OIFlashResearchStore:
    """Dedicated compact SQLite store for FLASH minute bars and crossings."""

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
                CREATE TABLE IF NOT EXISTS flash_bars_1m (
                    symbol TEXT NOT NULL,
                    minute_start_utc INTEGER NOT NULL,
                    oi_close REAL,
                    oi_observed_at_utc INTEGER,
                    oi_1m_pct REAL,
                    price_close REAL,
                    price_observed_at_utc INTEGER,
                    px_1m_pct REAL,
                    oi_observation_count INTEGER NOT NULL,
                    price_observation_count INTEGER NOT NULL,
                    valid_oi INTEGER NOT NULL,
                    valid_price INTEGER NOT NULL,
                    PRIMARY KEY (symbol, minute_start_utc)
                );
                CREATE INDEX IF NOT EXISTS idx_flash_bars_1m_minute
                    ON flash_bars_1m(minute_start_utc);
                CREATE TABLE IF NOT EXISTS flash_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    detected_at_utc INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK(direction IN ('positive', 'negative')),
                    oi_pct REAL NOT NULL,
                    px_pct REAL,
                    current_observed_at_utc INTEGER NOT NULL,
                    baseline_observed_at_utc INTEGER NOT NULL,
                    actual_window_seconds REAL NOT NULL,
                    threshold_pct REAL NOT NULL,
                    suppressed_by_cooldown INTEGER NOT NULL,
                    telegram_attempted INTEGER NOT NULL,
                    telegram_sent INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_flash_events_cooldown
                    ON flash_events(symbol, direction, suppressed_by_cooldown, detected_at_utc);
                CREATE INDEX IF NOT EXISTS idx_flash_events_detected
                    ON flash_events(detected_at_utc);
                """
            )
            connection.execute(
                "INSERT INTO schema_metadata(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def write_bars(self, bars: Sequence[FlashBar1m]) -> int:
        if not bars:
            return 0
        self.initialize()
        with self.connect() as connection:
            for bar in sorted(
                bars, key=lambda item: (item.minute_start_ms, item.symbol)
            ):
                previous = connection.execute(
                    "SELECT oi_close, price_close, valid_oi, valid_price "
                    "FROM flash_bars_1m WHERE symbol=? AND minute_start_utc=?",
                    (bar.symbol, bar.minute_start_ms - MINUTE_MS),
                ).fetchone()
                oi_pct = _consecutive_change(
                    bar.oi_close,
                    previous["oi_close"] if previous and previous["valid_oi"] else None,
                )
                px_pct = _consecutive_change(
                    bar.price_close,
                    previous["price_close"]
                    if previous and previous["valid_price"]
                    else None,
                )
                connection.execute(
                    """
                    INSERT INTO flash_bars_1m (
                        symbol, minute_start_utc, oi_close, oi_observed_at_utc,
                        oi_1m_pct, price_close, price_observed_at_utc, px_1m_pct,
                        oi_observation_count, price_observation_count, valid_oi, valid_price
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, minute_start_utc) DO UPDATE SET
                        oi_close=excluded.oi_close,
                        oi_observed_at_utc=excluded.oi_observed_at_utc,
                        oi_1m_pct=excluded.oi_1m_pct,
                        price_close=excluded.price_close,
                        price_observed_at_utc=excluded.price_observed_at_utc,
                        px_1m_pct=excluded.px_1m_pct,
                        oi_observation_count=excluded.oi_observation_count,
                        price_observation_count=excluded.price_observation_count,
                        valid_oi=excluded.valid_oi,
                        valid_price=excluded.valid_price
                    """,
                    (
                        bar.symbol,
                        bar.minute_start_ms,
                        bar.oi_close,
                        bar.oi_observed_at_ms,
                        oi_pct,
                        bar.price_close,
                        bar.price_observed_at_ms,
                        px_pct,
                        bar.oi_observation_count,
                        bar.price_observation_count,
                        int(bar.oi_close is not None),
                        int(bar.price_close is not None),
                    ),
                )
        return len(bars)

    def record_crossings(
        self, decisions: Sequence[tuple[OIFlashCandidate, bool]]
    ) -> tuple[PersistedFlashEvent, ...]:
        if not decisions:
            return ()
        self.initialize()
        persisted = []
        with self.connect() as connection:
            for candidate, suppressed in decisions:
                cursor = connection.execute(
                    """
                    INSERT INTO flash_events (
                        detected_at_utc, symbol, direction, oi_pct, px_pct,
                        current_observed_at_utc, baseline_observed_at_utc,
                        actual_window_seconds, threshold_pct, suppressed_by_cooldown,
                        telegram_attempted, telegram_sent
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
                    """,
                    (
                        to_epoch_ms(candidate.detected_at_utc),
                        candidate.symbol,
                        candidate.direction.value,
                        candidate.oi_pct,
                        candidate.px_pct,
                        to_epoch_ms(candidate.current_observed_at_utc),
                        to_epoch_ms(candidate.baseline_observed_at_utc),
                        candidate.actual_window_seconds,
                        candidate.threshold_pct,
                        int(suppressed),
                    ),
                )
                persisted.append(
                    PersistedFlashEvent(int(cursor.lastrowid), candidate, suppressed)
                )
        return tuple(persisted)

    def mark_telegram_attempted(self, event_ids: Sequence[int]) -> None:
        if not event_ids:
            return
        self.initialize()
        with self.connect() as connection:
            connection.executemany(
                "UPDATE flash_events SET telegram_attempted=1 WHERE id=?",
                ((event_id,) for event_id in event_ids),
            )

    def mark_telegram_results(self, results: Sequence[tuple[int, bool]]) -> None:
        if not results:
            return
        self.initialize()
        with self.connect() as connection:
            connection.executemany(
                "UPDATE flash_events SET telegram_sent=? WHERE id=?",
                ((int(sent), event_id) for event_id, sent in results),
            )

    def load_last_accepted(self) -> dict[tuple[str, OIFlashDirection], datetime]:
        self.initialize()
        with self.connect(read_only=True) as connection:
            rows = connection.execute(
                "SELECT symbol, direction, MAX(detected_at_utc) AS detected_at_utc "
                "FROM flash_events WHERE suppressed_by_cooldown=0 "
                "GROUP BY symbol, direction"
            ).fetchall()
        return {
            (row["symbol"], OIFlashDirection(row["direction"])): from_epoch_ms(
                row["detected_at_utc"]
            )
            for row in rows
        }

    def prune_bars(self, cutoff_utc: datetime, *, batch_size: int = 10_000) -> int:
        self.initialize()
        cutoff_ms = to_epoch_ms(cutoff_utc)
        removed = 0
        while True:
            with self.connect() as connection:
                cursor = connection.execute(
                    "DELETE FROM flash_bars_1m WHERE rowid IN ("
                    "SELECT rowid FROM flash_bars_1m WHERE minute_start_utc < ? LIMIT ?)",
                    (cutoff_ms, batch_size),
                )
                count = cursor.rowcount
            removed += count
            if count < batch_size:
                return removed


class OIFlashResearchTelemetry:
    """Non-blocking 1m aggregation over the existing OI and price feeds."""

    def __init__(
        self,
        path: str | Path,
        *,
        retention_days: float = 7.0,
        clock=lambda: datetime.now(timezone.utc),
        store: OIFlashResearchStore | None = None,
    ) -> None:
        if not math.isfinite(retention_days) or retention_days <= 0:
            raise ValueError("retention_days must be finite and positive")
        self.store = store or OIFlashResearchStore(path)
        self.retention_days = float(retention_days)
        self._clock = clock
        self._eligible_symbols: frozenset[str] = frozenset()
        self._bars: dict[tuple[str, int], _MutableFlashBar] = {}
        self._current_minute_ms: int | None = None
        self._lock = threading.RLock()
        self._commands: queue.Queue[tuple[str, object]] = queue.Queue()
        self._writer_thread: threading.Thread | None = None
        self._running = False
        self._last_prune_ms: int | None = None

    def start(self) -> None:
        if self._running:
            return
        try:
            self.store.initialize()
            logger.info("OI_FLASH_RESEARCH status=ready path=%s", self.store.path)
        except Exception:
            logger.exception("OI_FLASH_RESEARCH status=start_failed retry=true")
        self._running = True
        self._writer_thread = threading.Thread(
            target=self._writer_loop, name="oi-flash-research", daemon=True
        )
        self._writer_thread.start()

    def set_eligible_symbols(self, symbols: Sequence[str]) -> None:
        with self._lock:
            self._eligible_symbols = frozenset(symbol.upper() for symbol in symbols)

    def observe_oi(self, sample: RollingOISample) -> None:
        self._observe(
            sample.symbol,
            sample.oi_quantity,
            sample.observed_at_utc,
            is_price=False,
        )

    def observe_price(self, update: MarkPriceUpdate) -> None:
        self._observe(
            update.symbol,
            update.mark_price,
            update.exchange_time,
            is_price=True,
        )

    def _observe(
        self, symbol: str, value: float, observed_at_utc: datetime, *, is_price: bool
    ) -> None:
        if symbol.upper() not in self._eligible_symbols:
            return
        observed_ms = to_epoch_ms(observed_at_utc)
        minute_ms = observed_ms - observed_ms % MINUTE_MS
        closed: tuple[FlashBar1m, ...] = ()
        with self._lock:
            if (
                self._current_minute_ms is not None
                and minute_ms < self._current_minute_ms
            ):
                return
            if self._current_minute_ms is None or minute_ms > self._current_minute_ms:
                closed = self._close_before_locked(minute_ms)
                self._current_minute_ms = minute_ms
            bar = self._bars.setdefault(
                (symbol, minute_ms), _MutableFlashBar(symbol, minute_ms)
            )
            if is_price:
                bar.observe_price(value, observed_ms)
            else:
                bar.observe_oi(value, observed_ms)
        if closed and self._running:
            self._commands.put(("write", closed))

    def _close_before_locked(self, cutoff_ms: int) -> tuple[FlashBar1m, ...]:
        keys = [key for key in self._bars if key[1] < cutoff_ms]
        bars = tuple(self._bars.pop(key).freeze() for key in sorted(keys))
        return bars

    async def stop(self) -> None:
        await asyncio.to_thread(self.stop_sync)

    def stop_sync(self) -> None:
        if not self._running:
            return
        self._commands.put(("stop", ()))
        if self._writer_thread is not None:
            self._writer_thread.join()
        self._running = False
        logger.info("OI_FLASH_RESEARCH status=stopped")

    def _writer_loop(self) -> None:
        while True:
            command, payload = self._commands.get()
            if command == "stop":
                return
            try:
                bars = tuple(payload)  # type: ignore[arg-type]
                written = self.store.write_bars(bars)
                newest = max((bar.minute_start_ms for bar in bars), default=0)
                logger.info(
                    "OI_FLASH_RESEARCH status=flushed bars_written=%d minute_utc=%s",
                    written,
                    from_epoch_ms(newest).isoformat() if newest else "NA",
                )
                if (
                    self._last_prune_ms is None
                    or newest - self._last_prune_ms >= 3_600_000
                ):
                    cutoff = from_epoch_ms(newest) - timedelta(days=self.retention_days)
                    removed = self.store.prune_bars(cutoff)
                    self._last_prune_ms = newest
                    logger.info(
                        "OI_FLASH_RETENTION rows_removed=%d cutoff_utc=%s",
                        removed,
                        cutoff.isoformat(),
                    )
            except Exception:
                logger.exception("OI_FLASH_RESEARCH status=write_failed retry=true")


def _consecutive_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous <= 0:
        return None
    value = (current / previous - 1.0) * 100.0
    return value if math.isfinite(value) else None
