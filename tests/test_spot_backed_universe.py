from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from oitgbot.clients.binance_api import BinanceAPI
from oitgbot.models import BinanceRateLimit, CurrentOpenInterest, RollingOIWindowResult
from oitgbot.scheduler_jobs import SchedulerJobs
from oitgbot.services.current_oi_collector import CurrentOICollector
from oitgbot.services.oi_anomaly_15m_publisher import OIAnomaly15mPublisher
from oitgbot.services.price_state import PriceStateStore
from oitgbot.services.rate_limit_budget import RateLimitBudget
from oitgbot.services.report_formatter import ReportFormatter
from oitgbot.services.rolling_oi_store import RollingOIStore
from oitgbot.services.spot_backed_universe import (
    FuturesMarket,
    SpotBackedUniverse,
    SpotMarket,
    resolve_spot_backed_universe,
)

NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


def future(base: str) -> FuturesMarket:
    return FuturesMarket(f"{base}USDT", base)


def spot(base: str) -> SpotMarket:
    return SpotMarket(f"{base}USDT", base)


def resolved(futures: tuple[FuturesMarket, ...], spots: tuple[SpotMarket, ...]):
    return resolve_spot_backed_universe(futures, spots)


def test_exact_matches_and_takes_precedence_over_multiplier_normalization():
    result = resolved(
        (future("BTC"), future("1000PEPE")),
        (spot("BTC"), spot("1000PEPE"), spot("PEPE")),
    )
    assert [
        (item.futures_symbol, item.spot_symbol, item.match_type) for item in result.kept
    ] == [
        ("1000PEPEUSDT", "1000PEPEUSDT", "exact"),
        ("BTCUSDT", "BTCUSDT", "exact"),
    ]


def test_exact_numeric_token_names_are_never_stripped():
    bases = ("1INCH", "2Z", "0G", "4", "1MBABYDOGE")
    result = resolved(tuple(map(future, bases)), tuple(map(spot, bases)))
    assert {item.futures_base for item in result.kept} == set(bases)
    assert {item.match_type for item in result.kept} == {"exact"}


def test_only_explicit_multiplier_prefixes_with_active_targets_are_supported():
    result = resolved(
        (
            future("1000PEPE"),
            future("1000000MOG"),
            future("123ABC"),
            future("1000MISSING"),
            future("1000000ABSENT"),
            future("1MPEPE"),
        ),
        (spot("PEPE"), spot("MOG"), spot("ABC")),
    )
    assert [
        (item.futures_base, item.spot_base, item.multiplier) for item in result.kept
    ] == [
        ("1000000MOG", "MOG", 1_000_000),
        ("1000PEPE", "PEPE", 1_000),
    ]
    assert {item.futures_symbol for item in result.excluded} == {
        "1000MISSINGUSDT",
        "1000000ABSENTUSDT",
        "123ABCUSDT",
        "1MPEPEUSDT",
    }


def test_explicit_aliases_require_an_active_target():
    result = resolved(
        (future("DODOX"), future("LUNA2"), future("FUTUREONLY")),
        (spot("DODO"),),
    )
    assert [
        (item.futures_symbol, item.spot_symbol, item.match_type) for item in result.kept
    ] == [("DODOXUSDT", "DODOUSDT", "alias")]
    assert {item.futures_symbol for item in result.excluded} == {
        "FUTUREONLYUSDT",
        "LUNA2USDT",
    }


def test_binance_spot_parser_requires_trading_usdt_and_valid_ascii_names():
    api = BinanceAPI()
    api._spot_request = Mock(
        return_value={
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "status": "TRADING",
                },
                {
                    "symbol": "BTCFDUSD",
                    "baseAsset": "BTC",
                    "quoteAsset": "FDUSD",
                    "status": "TRADING",
                },
                {
                    "symbol": "OLDUSDT",
                    "baseAsset": "OLD",
                    "quoteAsset": "USDT",
                    "status": "BREAK",
                },
                {
                    "symbol": "ÅUSDT",
                    "baseAsset": "Å",
                    "quoteAsset": "USDT",
                    "status": "TRADING",
                },
            ]
        }
    )
    assert api.get_active_spot_usdt_markets() == [SpotMarket("BTCUSDT", "BTC")]
    api._spot_request.assert_called_once_with("/api/v3/exchangeInfo")
    api.close()


class UniverseAPI:
    def __init__(self) -> None:
        self.futures = [future("BTC"), future("FUTUREONLY")]
        self.spots = [spot("BTC")]
        self.fail_spot = False
        self.current_oi_calls: list[str] = []

    def get_perpetual_futures_markets(self):
        return self.futures

    def get_active_spot_usdt_markets(self):
        if self.fail_spot:
            raise ConnectionError("spot unavailable")
        return self.spots

    def get_current_open_interest(self, symbol: str):
        self.current_oi_calls.append(symbol)
        return CurrentOpenInterest(symbol, 1.0, NOW)


def jobs_for(api: UniverseAPI, universe: SpotBackedUniverse | None = None):
    return SchedulerJobs(
        api,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        ReportFormatter(),
        spot_backed_universe=universe,
    )


def test_initial_failure_never_falls_back_to_unfiltered_futures():
    api = UniverseAPI()
    api.fail_spot = True
    jobs = jobs_for(api)
    assert jobs.get_symbols_cached() == []
    assert jobs.get_symbols_cached() == []


def test_initial_failure_retries_next_cycle_and_collects_only_resolved_symbols():
    api = UniverseAPI()
    api.fail_spot = True
    jobs = jobs_for(api)
    assert jobs.get_symbols_cached() == []

    api.fail_spot = False
    symbols = jobs.get_symbols_cached()
    assert symbols == ["BTCUSDT"]

    collector = CurrentOICollector(
        api,
        PriceStateStore(symbols),
        RollingOIStore(),
        RateLimitBudget((BinanceRateLimit("REQUEST_WEIGHT", "MINUTE", 1, 2400),)),
        clock=lambda: NOW,
    )

    async def collect():
        try:
            return await collector.collect_cycle(symbols)
        finally:
            await collector.close()

    result = asyncio.run(collect())
    assert api.current_oi_calls == ["BTCUSDT"]
    assert result.symbols_requested == 1


def test_refresh_failure_retains_last_known_good_universe():
    api = UniverseAPI()
    universe = SpotBackedUniverse()
    jobs = jobs_for(api, universe)
    assert jobs.get_symbols_cached() == ["BTCUSDT"]
    api.fail_spot = True
    jobs._symbols_cache_ttl = 0
    assert jobs.get_symbols_cached() == ["BTCUSDT"]
    assert universe.spot_base_for("BTCUSDT") == "BTC"


def test_collector_boundary_receives_only_canonical_symbols():
    api = UniverseAPI()
    symbols = jobs_for(api).get_symbols_cached()
    collector = CurrentOICollector(
        api,
        PriceStateStore(symbols),
        RollingOIStore(),
        RateLimitBudget((BinanceRateLimit("REQUEST_WEIGHT", "MINUTE", 1, 2400),)),
        clock=lambda: NOW,
    )

    async def collect():
        try:
            return await collector.collect_cycle(symbols)
        finally:
            await collector.close()

    result = asyncio.run(collect())
    assert api.current_oi_calls == ["BTCUSDT"]
    assert result.symbols_requested == 1


def event(symbol: str):
    return SimpleNamespace(
        symbol=symbol,
        oi_quantity_change_pct=5.0,
        price_change_pct=1.0,
    )


def candidate(symbol: str, *, is_new: bool = False, rank: int = 1):
    return SimpleNamespace(
        symbol=symbol,
        is_eligible=True,
        is_new=is_new,
        rank=rank,
        z_score=4.0,
        oi_change_pct=2.0,
        price_change_pct=1.0,
    )


def test_5m_and_15m_spot_hints_are_plain_text_and_nonredundant():
    mapping = {"BTCUSDT": "BTC", "1000PEPEUSDT": "PEPE"}
    formatter = ReportFormatter(spot_base_lookup=mapping.get)
    btc = formatter.format_rolling_impulse(event("BTCUSDT"))
    pepe = formatter.format_rolling_impulse(event("1000PEPEUSDT"))
    anomaly = formatter.format_oi_anomaly_15m((candidate("1000PEPEUSDT", is_new=True),))
    btc_anomaly = formatter.format_oi_anomaly_15m((candidate("BTCUSDT"),))
    assert "BTC (S)" not in btc
    assert "BTC (S)" not in btc_anomaly
    assert "1000PEPEUSDT</a> · PEPE (S)" in pepe
    assert "1000PEPEUSDT</a> · PEPE (S)" in anomaly
    assert pepe.count("<a href=") == 1
    assert 'href="https://www.coinglass.com/tv/Binance_1000PEPEUSDT"' in pepe
    assert "🆕 NEW" in anomaly


def test_hint_does_not_change_input_order_or_futures_prop_identity():
    mapping = {"DODOXUSDT": "DODO", "BTCUSDT": "BTC"}
    formatter = ReportFormatter(spot_base_lookup=mapping.get)
    text = formatter.format_oi_anomaly_15m(
        (candidate("DODOXUSDT", rank=1), candidate("BTCUSDT", rank=2))
    )
    assert text.index("DODOXUSDT") < text.index("BTCUSDT")

    sender = SimpleNamespace(calls=[])

    async def send_if_not_empty(channel_id, message, **kwargs):
        sender.calls.append((channel_id, message, kwargs))
        return True

    sender.send_if_not_empty = send_if_not_empty
    publisher = OIAnomaly15mPublisher(
        sender,
        formatter,
        all_channel_id="all",
        prop_channel_id="prop",
        prop_symbols={"DODOXUSDT"},
    )
    assert asyncio.run(
        publisher.publish((candidate("DODOXUSDT"), candidate("BTCUSDT", rank=2)))
    ) == (1, 1)
    assert "DODOXUSDT</a> · DODO (S)" in sender.calls[1][1]
    assert "BTCUSDT" not in sender.calls[1][1]


def test_legacy_20m_formatter_uses_same_hint_without_changing_result():
    result = RollingOIWindowResult(
        symbol="LUNA2USDT",
        window_seconds=1200,
        available=True,
        unavailable_reason=None,
        latest_timestamp=NOW,
        baseline_timestamp=NOW,
        target_timestamp=NOW,
        actual_window_seconds=1200,
        baseline_offset_seconds=0,
        latest_oi_quantity=2,
        baseline_oi_quantity=1,
        oi_quantity_change_pct=100,
        latest_mark_price=1,
        baseline_mark_price=1,
        price_change_pct=0,
        latest_oi_value_usd=2,
        baseline_oi_value_usd=1,
        oi_value_change_pct=100,
    )
    text = ReportFormatter(
        spot_base_lookup={"LUNA2USDT": "LUNA"}.get
    ).format_rolling_top([result])
    assert "LUNA2USDT</a> · LUNA (S)" in text
