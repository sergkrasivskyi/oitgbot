from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException

from oitgbot.config import settings
from oitgbot.models import BinanceRateLimit, CurrentOpenInterest
from oitgbot.services.spot_backed_universe import FuturesMarket, SpotMarket

logger = logging.getLogger(__name__)


class BinanceAPI:
    def __init__(self) -> None:
        self.base_url = settings.binance_base_url.rstrip("/")
        self.spot_base_url = settings.binance_spot_base_url.rstrip("/")
        self.timeout = settings.http_timeout
        self.retries = settings.http_retries

        # Для ThreadPoolExecutor: окрема Session на кожен потік
        self._local = threading.local()

        # Невеликий запас під паралельні запити
        self._pool_connections = 20
        self._pool_maxsize = 20

    def _build_session(self) -> requests.Session:
        session = requests.Session()

        adapter = HTTPAdapter(
            pool_connections=self._pool_connections,
            pool_maxsize=self._pool_maxsize,
            max_retries=0,  # retry контролюємо вручну в _request()
        )

        session.mount("https://", adapter)
        session.mount("http://", adapter)

        session.headers.update(
            {
                "User-Agent": "oitgbot/1.0",
                "Accept": "application/json",
            }
        )

        return session

    def _get_session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._build_session()
            self._local.session = session
        return session

    def _request(self, endpoint: str, params: dict[str, str] | None = None) -> Any:
        return self._request_from(self.base_url, endpoint, params)

    def _spot_request(self, endpoint: str, params: dict[str, str] | None = None) -> Any:
        return self._request_from(self.spot_base_url, endpoint, params)

    def _request_from(
        self,
        base_url: str,
        endpoint: str,
        params: dict[str, str] | None = None,
    ) -> Any:
        url = f"{base_url}{endpoint}"
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            try:
                session = self._get_session()
                response = session.get(
                    url,
                    params=params or {},
                    timeout=(3.5, self.timeout),  # connect timeout, read timeout
                )
                response.raise_for_status()

                try:
                    return response.json()
                except ValueError as exc:
                    logger.error(
                        "Binance returned non-JSON: endpoint=%s params=%s status=%s error=%s",
                        endpoint,
                        params,
                        response.status_code,
                        exc,
                    )
                    raise

            except RequestException as exc:
                last_error = exc

                if attempt < self.retries:
                    sleep_s = 0.5 * (attempt + 1)
                    logger.warning(
                        "Binance request retry %d/%d: endpoint=%s params=%s error=%s sleep=%.1fs",
                        attempt + 1,
                        self.retries,
                        endpoint,
                        params,
                        exc,
                        sleep_s,
                    )
                    time.sleep(sleep_s)
                else:
                    logger.error(
                        "Binance request failed: endpoint=%s params=%s error=%s",
                        endpoint,
                        params,
                        exc,
                    )

            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.error(
                    "Unexpected Binance client error: endpoint=%s params=%s error=%s",
                    endpoint,
                    params,
                    exc,
                )
                break

        raise (
            last_error if last_error else RuntimeError("Unknown Binance request error")
        )

    def get_perpetual_futures_symbols(self) -> list[str]:
        return [market.symbol for market in self.get_perpetual_futures_markets()]

    def get_perpetual_futures_markets(self) -> list[FuturesMarket]:
        data = self._request("/fapi/v1/exchangeInfo")
        if not isinstance(data, dict):
            raise TypeError("futures exchangeInfo response must be an object")
        out: list[FuturesMarket] = []
        for item in data.get("symbols", []):
            if not isinstance(item, dict):
                continue
            if item.get("contractType") != "PERPETUAL":
                continue
            if item.get("status") != "TRADING":
                continue
            symbol = item.get("symbol")
            base_asset = item.get("baseAsset")
            if not self._valid_market_name(symbol) or not self._valid_market_name(
                base_asset
            ):
                continue
            if item.get("quoteAsset") != "USDT" or symbol != f"{base_asset}USDT":
                continue
            out.append(FuturesMarket(symbol, base_asset))
        return out

    def get_active_spot_usdt_markets(self) -> list[SpotMarket]:
        data = self._spot_request("/api/v3/exchangeInfo")
        if not isinstance(data, dict):
            raise TypeError("spot exchangeInfo response must be an object")
        out: list[SpotMarket] = []
        for item in data.get("symbols", []):
            if not isinstance(item, dict):
                continue
            if item.get("status") != "TRADING" or item.get("quoteAsset") != "USDT":
                continue
            symbol = item.get("symbol")
            base_asset = item.get("baseAsset")
            if not self._valid_market_name(symbol) or not self._valid_market_name(
                base_asset
            ):
                continue
            if symbol != f"{base_asset}USDT":
                continue
            out.append(SpotMarket(symbol, base_asset))
        return out

    @staticmethod
    def _valid_market_name(value: object) -> bool:
        return (
            isinstance(value, str)
            and bool(value)
            and value.isascii()
            and value.isalnum()
            and value.upper() == value
        )

    def get_current_open_interest(self, symbol: str) -> CurrentOpenInterest:
        requested_symbol = symbol.upper()
        data = self._request("/fapi/v1/openInterest", {"symbol": requested_symbol})
        return CurrentOpenInterest.from_binance_payload(data, requested_symbol)

    def get_rate_limits(self) -> list[BinanceRateLimit]:
        data = self._request("/fapi/v1/exchangeInfo")
        if not isinstance(data, dict):
            raise ValueError("exchangeInfo response must be an object")  # noqa: TRY004

        raw_rate_limits = data.get("rateLimits")
        if raw_rate_limits is None:
            return []
        if not isinstance(raw_rate_limits, list):
            raise ValueError("exchangeInfo rateLimits must be a list")  # noqa: TRY004

        return [
            BinanceRateLimit.from_binance_payload(rate_limit)
            for rate_limit in raw_rate_limits
        ]

    def get_request_weight_limits(self) -> list[BinanceRateLimit]:
        return [
            rate_limit
            for rate_limit in self.get_rate_limits()
            if rate_limit.rate_limit_type == "REQUEST_WEIGHT"
        ]

    def get_open_interest_history(
        self,
        symbol: str,
        period: str = "5m",
        limit: int = 2,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[dict]:
        params: dict[str, str] = {
            "symbol": symbol.upper(),
            "period": period,
            "limit": str(limit),
        }

        if start_time is not None:
            params["startTime"] = str(start_time)
        if end_time is not None:
            params["endTime"] = str(end_time)

        return self._request("/futures/data/openInterestHist", params)

    def get_klines(
        self, symbol: str, interval: str = "5m", limit: int = 2
    ) -> list[list]:
        params = {
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": str(limit),
        }
        return self._request("/fapi/v1/klines", params)

    def price_change_pct(
        self, symbol: str, interval: str = "5m", limit: int = 2
    ) -> float:
        limit = max(limit, 2)

        data = self.get_klines(symbol, interval=interval, limit=limit)
        if not data or len(data) < 2:
            return 0.0

        try:
            prev_close = float(data[-2][4])
            last_close = float(data[-1][4])
        except (IndexError, TypeError, ValueError):
            return 0.0

        if prev_close == 0:
            return 0.0

        return (last_close - prev_close) / prev_close * 100.0

    def price_change_20m_pct_via_5m(self, symbol: str) -> float:
        data = self.get_klines(symbol, interval="5m", limit=5)
        if not data or len(data) < 5:
            return 0.0

        try:
            first_close = float(data[0][4])
            last_close = float(data[-1][4])
        except (IndexError, TypeError, ValueError):
            return 0.0

        if first_close == 0:
            return 0.0

        return (last_close - first_close) / first_close * 100.0

    def close(self) -> None:
        session = getattr(self._local, "session", None)
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001, S110
                pass
            self._local.session = None
