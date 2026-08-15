from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest import IsolatedAsyncioTestCase, TestCase

from oitgbot.models import MarkPriceUpdate
from oitgbot.services.mark_price_stream import (
    MARK_PRICE_STREAM_URL,
    MarkPriceStream,
    parse_mark_price_message,
    reconnect_delay_seconds,
)
from oitgbot.services.price_state import PriceStateStore


RECEIVED_AT = datetime(2024, 1, 2, 3, 4, 6, tzinfo=timezone.utc)
EVENT_TIME_MS = 1_704_164_645_000


def price_update(
    symbol: str = "BTCUSDT",
    *,
    price: float = 42_000.0,
    exchange_time: datetime | None = None,
    received_at: datetime = RECEIVED_AT,
) -> MarkPriceUpdate:
    return MarkPriceUpdate.from_binance_payload(
        {
            "s": symbol,
            "p": str(price),
            "E": int(
                (
                    exchange_time
                    or datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
                ).timestamp()
                * 1000
            ),
        },
        received_at,
    )


class MarkPriceParserTests(TestCase):
    def test_parses_valid_all_market_array(self) -> None:
        message = json.dumps(
            [
                {"e": "markPriceUpdate", "E": EVENT_TIME_MS, "s": "BTCUSDT", "p": "42000.25"},
                {"e": "markPriceUpdate", "E": EVENT_TIME_MS, "s": "ETHUSDT", "p": "2200.5"},
            ]
        )

        result = parse_mark_price_message(message, RECEIVED_AT)

        self.assertEqual(0, result.malformed_entries)
        self.assertEqual(("BTCUSDT", "ETHUSDT"), tuple(x.symbol for x in result.updates))

    def test_parses_price_and_distinct_utc_timestamps(self) -> None:
        result = parse_mark_price_message(
            json.dumps(
                [{"E": EVENT_TIME_MS, "s": "BTCUSDT", "p": "42000.25"}]
            ),
            RECEIVED_AT,
        )

        update = result.updates[0]
        self.assertIsInstance(update.mark_price, float)
        self.assertEqual(42000.25, update.mark_price)
        self.assertEqual(
            datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            update.exchange_time,
        )
        self.assertEqual(RECEIVED_AT, update.received_at_utc)
        self.assertNotEqual(update.exchange_time, update.received_at_utc)

    def test_rejects_malformed_and_non_finite_prices(self) -> None:
        for invalid_price in ("abc", "NaN", "Infinity", "-Infinity", "0", "-1"):
            with self.subTest(price=invalid_price):
                result = parse_mark_price_message(
                    json.dumps(
                        [{"E": EVENT_TIME_MS, "s": "BTCUSDT", "p": invalid_price}]
                    ),
                    RECEIVED_AT,
                )
                self.assertEqual((), result.updates)
                self.assertEqual(1, result.malformed_entries)

    def test_rejects_missing_timestamp(self) -> None:
        result = parse_mark_price_message(
            json.dumps([{"s": "BTCUSDT", "p": "1"}]), RECEIVED_AT
        )

        self.assertEqual((), result.updates)
        self.assertEqual(1, result.malformed_entries)

    def test_rejects_missing_symbol(self) -> None:
        result = parse_mark_price_message(
            json.dumps([{"E": EVENT_TIME_MS, "p": "1"}]), RECEIVED_AT
        )

        self.assertEqual((), result.updates)
        self.assertEqual(1, result.malformed_entries)

    def test_malformed_entry_does_not_discard_valid_entries(self) -> None:
        result = parse_mark_price_message(
            json.dumps(
                [
                    {"E": EVENT_TIME_MS, "s": "BTCUSDT", "p": "not-a-price"},
                    {"E": EVENT_TIME_MS, "s": "ETHUSDT", "p": "2200"},
                ]
            ),
            RECEIVED_AT,
        )

        self.assertEqual(1, result.malformed_entries)
        self.assertEqual(("ETHUSDT",), tuple(x.symbol for x in result.updates))

    def test_rejects_non_array_message(self) -> None:
        with self.assertRaisesRegex(ValueError, "array"):
            parse_mark_price_message("{}", RECEIVED_AT)

    def test_direct_model_construction_enforces_invariants(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite positive"):
            MarkPriceUpdate(
                symbol="BTCUSDT",
                mark_price=float("nan"),
                exchange_time=RECEIVED_AT,
                received_at_utc=RECEIVED_AT,
            )


class PriceStateStoreTests(TestCase):
    def test_inserts_and_retrieves_symbol(self) -> None:
        store = PriceStateStore()
        update = price_update()

        self.assertTrue(store.update(update))
        self.assertIs(update, store.get("BTCUSDT"))

    def test_newer_update_replaces_older(self) -> None:
        store = PriceStateStore()
        older = price_update(price=41_000)
        newer = price_update(
            price=42_000,
            exchange_time=older.exchange_time + timedelta(seconds=1),
        )

        store.update(older)
        self.assertTrue(store.update(newer))

        self.assertIs(newer, store.get("BTCUSDT"))

    def test_older_and_duplicate_updates_do_not_replace_latest(self) -> None:
        store = PriceStateStore()
        latest = price_update(price=42_000)
        older = price_update(
            price=41_000,
            exchange_time=latest.exchange_time - timedelta(seconds=1),
        )
        duplicate = price_update(
            price=43_000,
            exchange_time=latest.exchange_time,
            received_at=RECEIVED_AT + timedelta(seconds=1),
        )

        store.update(latest)

        self.assertFalse(store.update(older))
        self.assertFalse(store.update(duplicate))
        self.assertIs(latest, store.get("BTCUSDT"))

    def test_returns_fresh_price_and_rejects_stale_price(self) -> None:
        store = PriceStateStore()
        update = price_update()
        store.update(update)

        self.assertIs(
            update,
            store.get_fresh(
                "BTCUSDT", RECEIVED_AT + timedelta(seconds=5), max_age_seconds=5
            ),
        )
        self.assertIsNone(
            store.get_fresh(
                "BTCUSDT",
                RECEIVED_AT + timedelta(seconds=5, microseconds=1),
                max_age_seconds=5,
            )
        )

    def test_filters_to_eligible_symbols(self) -> None:
        store = PriceStateStore({"BTCUSDT"})

        self.assertTrue(store.update(price_update("BTCUSDT")))
        self.assertFalse(store.update(price_update("ETHUSDT")))
        self.assertEqual({"BTCUSDT"}, set(store.snapshot()))

    def test_missing_symbol_is_unavailable(self) -> None:
        store = PriceStateStore()

        self.assertIsNone(store.get("MISSING"))
        self.assertIsNone(store.get_fresh("MISSING", RECEIVED_AT, 5))

    def test_snapshot_is_an_independent_mapping(self) -> None:
        store = PriceStateStore()
        update = price_update()
        store.update(update)

        snapshot = store.snapshot()
        snapshot.clear()

        self.assertIs(update, store.get("BTCUSDT"))

    def test_eligible_universe_can_be_refreshed_atomically(self) -> None:
        store = PriceStateStore({"BTCUSDT", "ETHUSDT"})
        store.update(price_update("BTCUSDT"))
        store.update(price_update("ETHUSDT"))

        store.set_eligible_symbols(["ethusdt", "SOLUSDT"])

        self.assertIsNone(store.get("BTCUSDT"))
        self.assertIsNotNone(store.get("ETHUSDT"))
        self.assertFalse(store.update(price_update("BTCUSDT")))
        self.assertTrue(store.update(price_update("SOLUSDT")))


class FakeWebSocket:
    def __init__(self, *messages: str | Exception) -> None:
        self.messages: asyncio.Queue[str | Exception] = asyncio.Queue()
        for message in messages:
            self.messages.put_nowait(message)
        self.closed = False

    async def recv(self) -> str:
        item = await self.messages.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.messages.put_nowait(ConnectionError("socket closed"))


class FakeConnection:
    def __init__(self, websocket: FakeWebSocket) -> None:
        self.websocket = websocket

    async def __aenter__(self) -> FakeWebSocket:
        return self.websocket

    async def __aexit__(self, *_args: object) -> None:
        await self.websocket.close()


class FakeConnector:
    def __init__(self, *websockets: FakeWebSocket) -> None:
        self.websockets = list(websockets)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, endpoint: str, **kwargs: object) -> FakeConnection:
        self.calls.append((endpoint, kwargs))
        if not self.websockets:
            raise ConnectionError("no fake connection")
        return FakeConnection(self.websockets.pop(0))


async def wait_until(predicate: object, timeout: float = 1.0) -> None:
    async def poll() -> None:
        while not predicate():  # type: ignore[operator]
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout)


class MarkPriceStreamLifecycleTests(IsolatedAsyncioTestCase):
    def test_endpoint_and_backoff_progression(self) -> None:
        self.assertEqual(
            "wss://fstream.binance.com/market/ws/!markPrice@arr@1s",
            MARK_PRICE_STREAM_URL,
        )
        self.assertEqual(
            [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0],
            [reconnect_delay_seconds(i) for i in range(7)],
        )

    async def test_reconnects_tracks_health_and_stops_gracefully(self) -> None:
        first = FakeWebSocket(ConnectionError("first connection failed"))
        second = FakeWebSocket(
            json.dumps(
                [{"E": EVENT_TIME_MS, "s": "BTCUSDT", "p": "42000"}]
            )
        )
        connector = FakeConnector(first, second)
        delays: list[float] = []

        async def immediate_sleep(delay: float) -> None:
            delays.append(delay)

        store = PriceStateStore({"BTCUSDT"})
        stream = MarkPriceStream(
            store,
            connect_factory=connector,
            stale_after_seconds=60,
            sleep=immediate_sleep,
            random_value=lambda: 0.5,
        )
        task = asyncio.create_task(stream.run())
        await wait_until(lambda: store.get("BTCUSDT") is not None)

        update = store.get("BTCUSDT")
        assert update is not None
        health = stream.health(update.received_at_utc, max_age_seconds=5)
        self.assertTrue(health.connected)
        self.assertFalse(health.stale)
        self.assertEqual(1, health.reconnect_count)
        self.assertEqual([1.0], delays)
        self.assertEqual(MARK_PRICE_STREAM_URL, connector.calls[0][0])
        self.assertEqual(20, connector.calls[0][1]["ping_interval"])
        self.assertEqual(20, connector.calls[0][1]["ping_timeout"])

        stale_health = stream.health(
            update.received_at_utc + timedelta(seconds=6), max_age_seconds=5
        )
        self.assertTrue(stale_health.stale)

        await stream.stop()
        await task

        self.assertFalse(
            stream.health(update.received_at_utc, max_age_seconds=5).connected
        )
        self.assertTrue(second.closed)

    async def test_run_can_be_cancelled(self) -> None:
        websocket = FakeWebSocket()
        stream = MarkPriceStream(
            PriceStateStore(),
            connect_factory=FakeConnector(websocket),
            stale_after_seconds=60,
        )
        task = asyncio.create_task(stream.run())
        await wait_until(
            lambda: stream.health(RECEIVED_AT, max_age_seconds=5).connected
        )

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertFalse(
            stream.health(RECEIVED_AT, max_age_seconds=5).connected
        )

    async def test_run_can_be_cancelled_during_reconnect_backoff(self) -> None:
        stream = MarkPriceStream(
            PriceStateStore(),
            connect_factory=FakeConnector(),
            random_value=lambda: 0.5,
        )
        task = asyncio.create_task(stream.run())
        await wait_until(
            lambda: stream.health(RECEIVED_AT, max_age_seconds=5).reconnect_count
            == 1
        )

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertFalse(
            stream.health(RECEIVED_AT, max_age_seconds=5).connected
        )

    async def test_no_valid_frames_triggers_stale_reconnect(self) -> None:
        websocket = FakeWebSocket()
        connector = FakeConnector(websocket)
        stream: MarkPriceStream

        async def stop_during_backoff(_delay: float) -> None:
            await stream.stop()

        stream = MarkPriceStream(
            PriceStateStore(),
            connect_factory=connector,
            stale_after_seconds=0.001,
            sleep=stop_during_backoff,
            random_value=lambda: 0.5,
        )

        await stream.run()

        health = stream.health(datetime.now(timezone.utc), max_age_seconds=5)
        self.assertEqual(1, health.reconnect_count)
        self.assertTrue(health.stale)
        self.assertIn("StalePriceStreamError", health.last_error or "")
