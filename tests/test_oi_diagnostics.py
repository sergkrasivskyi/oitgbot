from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from oitgbot.services.oi_diagnostics import build_oi_window_diagnostic
from oitgbot.services.oi_scanner import OIScanner


UTC = timezone.utc


def epoch_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


class FakeBinanceAPI:
    def __init__(self, history: list[dict[str, object]]) -> None:
        self.history = history

    def get_open_interest_history(self, symbol: str, period: str, limit: int) -> list[dict[str, object]]:
        return self.history


class OIWindowDiagnosticTests(unittest.TestCase):
    def test_valid_5m_timestamps_calculate_gap_and_age(self) -> None:
        start = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
        end = start + timedelta(seconds=300)
        diagnostic = build_oi_window_diagnostic(
            symbol="ABCUSDT",
            window="5m",
            start_timestamp=epoch_ms(start),
            end_timestamp=epoch_ms(end),
            start_oi=100.0,
            end_oi=106.0,
            change_pct=6.0,
            expected_gap_seconds=300,
            scan_utc=end + timedelta(seconds=18),
        )

        self.assertEqual(diagnostic.actual_gap_seconds, 300)
        self.assertEqual(diagnostic.latest_age_seconds, 18)
        self.assertEqual(diagnostic.anomaly_codes(), [])

    def test_stale_valid_5m_preserves_percentage(self) -> None:
        start = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
        end = start + timedelta(minutes=5)
        scan = datetime(2026, 8, 15, 12, 10, 20, tzinfo=UTC)
        start_oi = 1_000.0
        end_oi = 1_060.0
        original_pct = (end_oi - start_oi) / start_oi * 100.0
        diagnostic = build_oi_window_diagnostic(
            symbol="ABCUSDT",
            window="5m",
            start_timestamp=epoch_ms(start),
            end_timestamp=epoch_ms(end),
            start_oi=start_oi,
            end_oi=end_oi,
            change_pct=original_pct,
            expected_gap_seconds=300,
            scan_utc=scan,
        )

        self.assertEqual(diagnostic.latest_age_seconds, 320)
        self.assertEqual(diagnostic.change_pct, original_pct)

    def test_valid_20m_timestamps_calculate_gap(self) -> None:
        start = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
        samples = [start + timedelta(minutes=5 * index) for index in range(5)]
        diagnostic = build_oi_window_diagnostic(
            symbol="ABCUSDT",
            window="20m",
            start_timestamp=epoch_ms(samples[0]),
            end_timestamp=epoch_ms(samples[-1]),
            start_oi=100.0,
            end_oi=104.0,
            change_pct=4.0,
            expected_gap_seconds=1200,
            scan_utc=samples[-1] + timedelta(seconds=10),
        )

        self.assertEqual(diagnostic.actual_gap_seconds, 1200)
        self.assertFalse(diagnostic.has_gap_mismatch)

    def test_gap_mismatch_is_detectable(self) -> None:
        start = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
        diagnostic = build_oi_window_diagnostic(
            symbol="ABCUSDT",
            window="5m",
            start_timestamp=epoch_ms(start),
            end_timestamp=epoch_ms(start + timedelta(seconds=240)),
            start_oi=100.0,
            end_oi=101.0,
            change_pct=1.0,
            expected_gap_seconds=300,
            scan_utc=start + timedelta(seconds=250),
        )

        self.assertTrue(diagnostic.has_gap_mismatch)
        self.assertIn("gap_mismatch", diagnostic.anomaly_codes())

    def test_missing_or_malformed_timestamp_never_becomes_now(self) -> None:
        scan = datetime(2026, 8, 15, 12, 10, 20, tzinfo=UTC)
        diagnostic = build_oi_window_diagnostic(
            symbol="ABCUSDT",
            window="5m",
            start_timestamp=None,
            end_timestamp="not-a-timestamp",
            start_oi=100.0,
            end_oi=101.0,
            change_pct=1.0,
            expected_gap_seconds=300,
            scan_utc=scan,
        )

        self.assertIsNone(diagnostic.start_utc)
        self.assertIsNone(diagnostic.end_utc)
        self.assertIsNone(diagnostic.latest_age_seconds)
        self.assertIn("start_timestamp_missing", diagnostic.anomaly_codes())
        self.assertIn("end_timestamp_malformed", diagnostic.anomaly_codes())

    def test_scanner_keeps_existing_5m_formula_while_attaching_timestamp_data(self) -> None:
        start = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
        end = start + timedelta(minutes=5)
        scanner = OIScanner(
            FakeBinanceAPI(
                [
                    {"timestamp": epoch_ms(start), "sumOpenInterestValue": "1000"},
                    {"timestamp": epoch_ms(end), "sumOpenInterestValue": "1060"},
                ]
            )
        )

        row, diagnostic = scanner._fetch_oi_5m_for_symbol("abcusdt")

        self.assertIsNotNone(row)
        self.assertIsNotNone(diagnostic)
        self.assertEqual(row.oi_pct, 6.0)
        self.assertEqual(diagnostic.change_pct, 6.0)
        self.assertEqual(diagnostic.actual_gap_seconds, 300)


if __name__ == "__main__":
    unittest.main()
