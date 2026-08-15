from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException

from oitgbot.config import settings

logger = logging.getLogger(__name__)


class BinanceAPI:
    def __init__(self) -> None:
        self.base_url = settings.binance_base_url.rstrip("/")
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
        url = f"{self.base_url}{endpoint}"
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

            except Exception as exc:
                last_error = exc
                logger.error(
                    "Unexpected Binance client error: endpoint=%s params=%s error=%s",
                    endpoint,
                    params,
                    exc,
                )
                break

        raise last_error if last_error else RuntimeError("Unknown Binance request error")

    def get_perpetual_futures_symbols(self) -> list[str]:
        data = self._request("/fapi/v1/exchangeInfo")
        out: list[str] = []

        for item in data.get("symbols", []):
            if item.get("contractType") != "PERPETUAL":
                continue
            if item.get("status") != "TRADING":
                continue

            symbol = item.get("symbol")
            if not symbol:
                continue

            # Працюємо тільки з USDT-парами
            if not symbol.endswith("USDT"):
                continue

            # Відсікаємо не-ASCII тікери (ієрогліфи тощо)
            if not symbol.isascii():
                continue

            out.append(symbol)

        return out

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

    def get_klines(self, symbol: str, interval: str = "5m", limit: int = 2) -> list[list]:
        params = {
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": str(limit),
        }
        return self._request("/fapi/v1/klines", params)

    def price_change_pct(self, symbol: str, interval: str = "5m", limit: int = 2) -> float:
        if limit < 2:
            limit = 2

        data = self.get_klines(symbol, interval=interval, limit=limit)
        if not data or len(data) < 2:
            return 0.0

        try:
            prev_close = float(data[-2][4])
            last_close = float(data[-1][4])
        except Exception:
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
        except Exception:
            return 0.0

        if first_close == 0:
            return 0.0

        return (last_close - first_close) / first_close * 100.0

    def close(self) -> None:
        session = getattr(self._local, "session", None)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
            self._local.session = None