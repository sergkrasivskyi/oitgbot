from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass

logger = logging.getLogger("oitgbot.spot_backed_universe")

EXPLICIT_BASE_ALIASES = {
    "DODOX": "DODO",
    "LUNA2": "LUNA",
}
MULTIPLIER_PREFIXES = (
    ("1000000", 1_000_000),
    ("1000", 1_000),
)


@dataclass(frozen=True, slots=True)
class FuturesMarket:
    symbol: str
    base_asset: str


@dataclass(frozen=True, slots=True)
class SpotMarket:
    symbol: str
    base_asset: str


@dataclass(frozen=True, slots=True)
class SpotBackedMarket:
    futures_symbol: str
    futures_base: str
    spot_symbol: str
    spot_base: str
    match_type: str
    multiplier: int | None = None


@dataclass(frozen=True, slots=True)
class UniverseExclusion:
    futures_symbol: str
    reason: str = "no_active_spot_usdt"


@dataclass(frozen=True, slots=True)
class SpotBackedUniverseResolution:
    futures_eligible: int
    active_spot_usdt: int
    kept: tuple[SpotBackedMarket, ...]
    excluded: tuple[UniverseExclusion, ...]

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(item.futures_symbol for item in self.kept)

    def count(self, match_type: str) -> int:
        return sum(item.match_type == match_type for item in self.kept)


def resolve_spot_backed_universe(
    futures_markets: Iterable[FuturesMarket],
    spot_markets: Iterable[SpotMarket],
) -> SpotBackedUniverseResolution:
    futures = tuple(sorted(futures_markets, key=lambda item: item.symbol))
    spots = tuple(sorted(spot_markets, key=lambda item: item.symbol))
    spot_by_symbol = {item.symbol: item for item in spots}
    kept: list[SpotBackedMarket] = []
    excluded: list[UniverseExclusion] = []

    for future in futures:
        exact = spot_by_symbol.get(future.symbol)
        if exact is not None and exact.base_asset == future.base_asset:
            kept.append(
                SpotBackedMarket(
                    future.symbol,
                    future.base_asset,
                    exact.symbol,
                    exact.base_asset,
                    "exact",
                )
            )
            continue

        alias_base = EXPLICIT_BASE_ALIASES.get(future.base_asset)
        if alias_base is not None:
            target = spot_by_symbol.get(f"{alias_base}USDT")
            if target is not None and target.base_asset == alias_base:
                kept.append(
                    SpotBackedMarket(
                        future.symbol,
                        future.base_asset,
                        target.symbol,
                        target.base_asset,
                        "alias",
                    )
                )
                continue

        multiplier_match: SpotBackedMarket | None = None
        for prefix, multiplier in MULTIPLIER_PREFIXES:
            if not future.base_asset.startswith(prefix):
                continue
            target_base = future.base_asset[len(prefix) :]
            if not target_base:
                continue
            target = spot_by_symbol.get(f"{target_base}USDT")
            if target is not None and target.base_asset == target_base:
                multiplier_match = SpotBackedMarket(
                    future.symbol,
                    future.base_asset,
                    target.symbol,
                    target.base_asset,
                    "multiplier",
                    multiplier,
                )
                break
        if multiplier_match is not None:
            kept.append(multiplier_match)
        else:
            excluded.append(UniverseExclusion(future.symbol))

    return SpotBackedUniverseResolution(
        futures_eligible=len(futures),
        active_spot_usdt=len(spots),
        kept=tuple(kept),
        excluded=tuple(excluded),
    )


class SpotBackedUniverse:
    """Atomically refresh and expose the last known-good resolved universe."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._resolution: SpotBackedUniverseResolution | None = None
        self._spot_bases: dict[str, str] = {}

    def refresh(self, binance_api: object) -> SpotBackedUniverseResolution:
        futures = binance_api.get_perpetual_futures_markets()  # type: ignore[attr-defined]
        spots = binance_api.get_active_spot_usdt_markets()  # type: ignore[attr-defined]
        if not futures or not spots:
            raise RuntimeError("exchange-info universe must not be empty")
        resolution = resolve_spot_backed_universe(futures, spots)
        if not resolution.kept:
            raise RuntimeError("resolved spot-backed universe must not be empty")
        with self._lock:
            self._resolution = resolution
            self._spot_bases = {
                item.futures_symbol: item.spot_base for item in resolution.kept
            }
        logger.info(
            "SPOT_BACKED_UNIVERSE futures=%d spot_usdt=%d exact=%d multiplier=%d "
            "alias=%d excluded=%d final=%d",
            resolution.futures_eligible,
            resolution.active_spot_usdt,
            resolution.count("exact"),
            resolution.count("multiplier"),
            resolution.count("alias"),
            len(resolution.excluded),
            len(resolution.kept),
        )
        for item in resolution.kept:
            if item.match_type != "exact":
                logger.info(
                    "SPOT_BACKED_UNIVERSE_KEEP futures=%s spot=%s match=%s",
                    item.futures_symbol,
                    item.spot_symbol,
                    item.match_type,
                )
        if resolution.excluded:
            sample = resolution.excluded[:20]
            logger.info(
                "SPOT_BACKED_UNIVERSE_DROP count=%d sample=%s truncated=%s",
                len(resolution.excluded),
                ",".join(item.futures_symbol for item in sample),
                len(sample) < len(resolution.excluded),
            )
            logger.debug(
                "SPOT_BACKED_UNIVERSE_DROP_ALL symbols=%s",
                ",".join(item.futures_symbol for item in resolution.excluded),
            )
        return resolution

    def last_known_good(self) -> SpotBackedUniverseResolution | None:
        with self._lock:
            return self._resolution

    def spot_base_for(self, futures_symbol: str) -> str | None:
        with self._lock:
            return self._spot_bases.get(futures_symbol.upper())
