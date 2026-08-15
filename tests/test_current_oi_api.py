from __future__ import annotations

from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import Mock

from requests.exceptions import ConnectionError

from oitgbot.clients.binance_api import BinanceAPI
from oitgbot.models import BinanceRateLimit, CurrentOpenInterest


class CurrentOpenInterestAPITests(TestCase):
    def setUp(self) -> None:
        self.client = BinanceAPI()
        self.client._request = Mock()

    def test_returns_strict_current_open_interest_model(self) -> None:
        self.client._request.return_value = {
            "symbol": "BTCUSDT",
            "openInterest": "10659.509",
            "time": 1589437530011,
        }

        result = self.client.get_current_open_interest("btcusdt")

        self.assertIsInstance(result, CurrentOpenInterest)
        self.assertEqual("BTCUSDT", result.symbol)
        self.assertEqual(10659.509, result.oi_quantity)
        self.assertEqual(
            datetime(2020, 5, 14, 6, 25, 30, 11000, tzinfo=timezone.utc),
            result.exchange_time,
        )
        self.client._request.assert_called_once_with(
            "/fapi/v1/openInterest", {"symbol": "BTCUSDT"}
        )

    def test_rejects_missing_open_interest(self) -> None:
        self.client._request.return_value = {"symbol": "BTCUSDT", "time": 1}

        with self.assertRaisesRegex(ValueError, "missing openInterest"):
            self.client.get_current_open_interest("BTCUSDT")

    def test_rejects_non_finite_or_malformed_open_interest(self) -> None:
        for open_interest in ("abc", "NaN", "Infinity", float("nan"), float("inf")):
            with self.subTest(open_interest=open_interest):
                self.client._request.return_value = {
                    "symbol": "BTCUSDT",
                    "openInterest": open_interest,
                    "time": 1,
                }

                with self.assertRaisesRegex(ValueError, "finite non-negative"):
                    self.client.get_current_open_interest("BTCUSDT")

    def test_rejects_missing_or_malformed_time(self) -> None:
        for payload in (
            {"symbol": "BTCUSDT", "openInterest": "1"},
            {"symbol": "BTCUSDT", "openInterest": "1", "time": "soon"},
            {"symbol": "BTCUSDT", "openInterest": "1", "time": 1.5},
        ):
            with self.subTest(payload=payload):
                self.client._request.return_value = payload

                with self.assertRaises(ValueError):
                    self.client.get_current_open_interest("BTCUSDT")

    def test_rejects_response_symbol_mismatch(self) -> None:
        self.client._request.return_value = {
            "symbol": "ETHUSDT",
            "openInterest": "1",
            "time": 1,
        }

        with self.assertRaisesRegex(ValueError, "does not match"):
            self.client.get_current_open_interest("BTCUSDT")

    def test_request_failure_propagates(self) -> None:
        request_error = ConnectionError("network unavailable")
        self.client._request.side_effect = request_error

        with self.assertRaises(ConnectionError) as raised:
            self.client.get_current_open_interest("BTCUSDT")

        self.assertIs(request_error, raised.exception)


class BinanceRateLimitAPITests(TestCase):
    def setUp(self) -> None:
        self.client = BinanceAPI()
        self.client._request = Mock()

    def test_returns_all_valid_rate_limits(self) -> None:
        self.client._request.return_value = {
            "rateLimits": [
                {
                    "rateLimitType": "REQUEST_WEIGHT",
                    "interval": "MINUTE",
                    "intervalNum": 1,
                    "limit": 2400,
                },
                {
                    "rateLimitType": "ORDERS",
                    "interval": "SECOND",
                    "intervalNum": "10",
                    "limit": "300",
                },
            ]
        }

        result = self.client.get_rate_limits()

        self.assertEqual(
            [
                BinanceRateLimit("REQUEST_WEIGHT", "MINUTE", 1, 2400),
                BinanceRateLimit("ORDERS", "SECOND", 10, 300),
            ],
            result,
        )
        self.client._request.assert_called_once_with("/fapi/v1/exchangeInfo")

    def test_returns_every_request_weight_limit(self) -> None:
        self.client._request.return_value = {
            "rateLimits": [
                {
                    "rateLimitType": "REQUEST_WEIGHT",
                    "interval": "SECOND",
                    "intervalNum": 10,
                    "limit": 100,
                },
                {
                    "rateLimitType": "ORDERS",
                    "interval": "MINUTE",
                    "intervalNum": 1,
                    "limit": 1200,
                },
                {
                    "rateLimitType": "REQUEST_WEIGHT",
                    "interval": "MINUTE",
                    "intervalNum": 1,
                    "limit": 2400,
                },
            ]
        }

        result = self.client.get_request_weight_limits()

        self.assertEqual(
            [
                BinanceRateLimit("REQUEST_WEIGHT", "SECOND", 10, 100),
                BinanceRateLimit("REQUEST_WEIGHT", "MINUTE", 1, 2400),
            ],
            result,
        )

    def test_missing_rate_limits_returns_empty_list(self) -> None:
        self.client._request.return_value = {"symbols": []}

        self.assertEqual([], self.client.get_rate_limits())

    def test_rejects_malformed_rate_limit_numbers(self) -> None:
        self.client._request.return_value = {
            "rateLimits": [
                {
                    "rateLimitType": "REQUEST_WEIGHT",
                    "interval": "MINUTE",
                    "intervalNum": 1,
                    "limit": "not-a-number",
                }
            ]
        }

        with self.assertRaisesRegex(ValueError, "limit"):
            self.client.get_rate_limits()
