from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from websockets.asyncio.client import connect

from oitgbot.models import MarkPriceUpdate
from oitgbot.services.price_state import PriceStateStore

logger = logging.getLogger(__name__)

MARK_PRICE_STREAM_URL = (
    "wss://fstream.binance.com/market/ws/!markPrice@arr@1s"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{field_name} must be a timezone-aware UTC datetime")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class MarkPriceParseResult:
    updates: tuple[MarkPriceUpdate, ...]
    malformed_entries: int


def parse_mark_price_message(
    message: str | bytes,
    received_at_utc: datetime | None = None,
) -> MarkPriceParseResult:
    """Parse one raw all-market frame while isolating malformed entries."""

    receipt_time = _validate_utc(
        received_at_utc if received_at_utc is not None else _utc_now(),
        "received_at_utc",
    )
    try:
        payload = json.loads(message)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        raise ValueError("mark price message must be valid JSON") from exc
    if not isinstance(payload, list):
        raise ValueError("mark price message must contain an array")

    updates: list[MarkPriceUpdate] = []
    malformed_entries = 0
    for entry in payload:
        try:
            updates.append(MarkPriceUpdate.from_binance_payload(entry, receipt_time))
        except ValueError:
            malformed_entries += 1

    return MarkPriceParseResult(tuple(updates), malformed_entries)


@dataclass(frozen=True, slots=True)
class PriceStreamHealth:
    connected: bool
    last_message_received_at: datetime | None
    last_valid_update_at: datetime | None
    reconnect_count: int
    stale: bool
    last_error: str | None


class StalePriceStreamError(RuntimeError):
    pass


def reconnect_delay_seconds(
    attempt: int,
    *,
    base_seconds: float = 1.0,
    cap_seconds: float = 30.0,
    jitter_value: float = 0.5,
) -> float:
    """Return bounded exponential backoff with +/-20 percent jitter."""

    if attempt < 0:
        raise ValueError("attempt must be non-negative")
    if base_seconds <= 0 or cap_seconds <= 0 or base_seconds > cap_seconds:
        raise ValueError("backoff bounds must be positive and ordered")
    if not 0 <= jitter_value <= 1:
        raise ValueError("jitter_value must be between zero and one")

    unjittered = min(cap_seconds, base_seconds * (2**attempt))
    jittered = unjittered * (0.8 + 0.4 * jitter_value)
    return min(cap_seconds, jittered)


class MarkPriceStream:
    """Maintain Binance's all-market mark-price stream and update a store."""

    def __init__(
        self,
        store: PriceStateStore,
        *,
        endpoint: str = MARK_PRICE_STREAM_URL,
        stale_after_seconds: float = 5.0,
        stable_reset_seconds: float = 60.0,
        connect_factory: Callable[..., Any] = connect,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        if stale_after_seconds <= 0 or not math.isfinite(stale_after_seconds):
            raise ValueError("stale_after_seconds must be finite and positive")
        if stable_reset_seconds <= 0 or not math.isfinite(stable_reset_seconds):
            raise ValueError("stable_reset_seconds must be finite and positive")

        self.store = store
        self.endpoint = endpoint
        self.stale_after_seconds = stale_after_seconds
        self.stable_reset_seconds = stable_reset_seconds
        self._connect_factory = connect_factory
        self._clock = clock
        self._sleep = sleep
        self._random_value = random_value
        self._stop_event = asyncio.Event()
        self._active_socket: Any = None
        self._state_lock = threading.RLock()
        self._connected = False
        self._last_message_received_at: datetime | None = None
        self._last_valid_update_at: datetime | None = None
        self._reconnect_count = 0
        self._last_error: str | None = None

    async def run(self) -> None:
        failure_streak = 0
        try:
            while not self._stop_event.is_set():
                connected_at: datetime | None = None
                try:
                    async with self._connect_factory(
                        self.endpoint,
                        ping_interval=20,
                        ping_timeout=20,
                        close_timeout=10,
                    ) as websocket:
                        self._active_socket = websocket
                        connected_at = _validate_utc(self._clock(), "clock")
                        self._set_connected(True)
                        logger.info("PRICE_STREAM_STATUS status=connected")
                        await self._consume(websocket, connected_at)
                        if self._stop_event.is_set():
                            break
                        raise ConnectionError("mark price stream ended")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._set_disconnected(exc)
                    if self._stop_event.is_set():
                        break

                    if connected_at is not None and self._was_stable(connected_at):
                        failure_streak = 0
                    delay = reconnect_delay_seconds(
                        failure_streak, jitter_value=self._random_value()
                    )
                    failure_streak += 1
                    with self._state_lock:
                        self._reconnect_count += 1
                        reconnect_count = self._reconnect_count
                    logger.info(
                        "PRICE_STREAM_STATUS status=reconnecting attempt=%d delay=%.3f",
                        reconnect_count,
                        delay,
                    )
                    await self._sleep_or_stop(delay)
                finally:
                    self._active_socket = None
                    self._set_connected(False)
        finally:
            self._set_connected(False)
            logger.info("PRICE_STREAM_STATUS status=stopped")

    async def stop(self) -> None:
        self._stop_event.set()
        websocket = self._active_socket
        if websocket is not None:
            await websocket.close()

    async def close(self) -> None:
        await self.stop()

    def health(
        self, reference_utc: datetime, max_age_seconds: float
    ) -> PriceStreamHealth:
        reference = _validate_utc(reference_utc, "reference_utc")
        if (
            isinstance(max_age_seconds, bool)
            or not isinstance(max_age_seconds, (int, float))
            or max_age_seconds < 0
            or not math.isfinite(max_age_seconds)
        ):
            raise ValueError("max_age_seconds must be finite and non-negative")

        with self._state_lock:
            last_valid = self._last_valid_update_at
            stale = (
                last_valid is None
                or (reference - last_valid).total_seconds() < 0
                or (reference - last_valid).total_seconds() > max_age_seconds
            )
            return PriceStreamHealth(
                connected=self._connected,
                last_message_received_at=self._last_message_received_at,
                last_valid_update_at=last_valid,
                reconnect_count=self._reconnect_count,
                stale=stale,
                last_error=self._last_error,
            )

    async def _consume(self, websocket: Any, connected_at: datetime) -> None:
        while not self._stop_event.is_set():
            now = _validate_utc(self._clock(), "clock")
            with self._state_lock:
                freshness_start = max(
                    self._last_valid_update_at or connected_at,
                    connected_at,
                )
            remaining = self.stale_after_seconds - (now - freshness_start).total_seconds()
            if remaining <= 0:
                raise StalePriceStreamError("no valid mark price frames received")

            try:
                message = await asyncio.wait_for(websocket.recv(), timeout=remaining)
            except TimeoutError as exc:
                raise StalePriceStreamError(
                    "no valid mark price frames received"
                ) from exc

            received_at = _validate_utc(self._clock(), "clock")
            with self._state_lock:
                self._last_message_received_at = received_at

            try:
                result = parse_mark_price_message(message, received_at)
            except ValueError:
                logger.warning(
                    "PRICE_STREAM_WARNING malformed_entries=1 valid_entries=0"
                )
                continue

            for update in result.updates:
                self.store.update(update)
            if result.updates:
                with self._state_lock:
                    self._last_valid_update_at = received_at
            if result.malformed_entries:
                logger.warning(
                    "PRICE_STREAM_WARNING malformed_entries=%d valid_entries=%d",
                    result.malformed_entries,
                    len(result.updates),
                )

    async def _sleep_or_stop(self, delay: float) -> None:
        sleep_task = asyncio.create_task(self._sleep(delay))
        stop_task = asyncio.create_task(self._stop_event.wait())
        tasks = {sleep_task, stop_task}
        try:
            done, _pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                task.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _was_stable(self, connected_at: datetime) -> bool:
        with self._state_lock:
            return (
                self._last_valid_update_at is not None
                and (
                    self._last_valid_update_at - connected_at
                ).total_seconds()
                >= self.stable_reset_seconds
            )

    def _set_connected(self, connected: bool) -> None:
        with self._state_lock:
            self._connected = connected

    def _set_disconnected(self, error: Exception) -> None:
        with self._state_lock:
            self._connected = False
            self._last_error = f"{type(error).__name__}: {error}"
        logger.info(
            "PRICE_STREAM_STATUS status=disconnected reason=%s", type(error).__name__
        )
