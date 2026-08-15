from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tools.live_oi_probe import boundary_lag, floor_five_minute_boundary, legacy_change_pct


UTC = timezone.utc


class LiveOIProbeHelperTests(unittest.TestCase):
    def test_five_minute_boundary_rounds_down(self) -> None:
        probe = datetime(2026, 8, 15, 14, 13, 42, tzinfo=UTC)
        self.assertEqual(
            floor_five_minute_boundary(probe),
            datetime(2026, 8, 15, 14, 10, tzinfo=UTC),
        )

    def test_exact_boundary_stays_unchanged(self) -> None:
        probe = datetime(2026, 8, 15, 14, 10, tzinfo=UTC)
        self.assertEqual(floor_five_minute_boundary(probe), probe)

    def test_boundary_lag_zero_buckets(self) -> None:
        boundary = datetime(2026, 8, 15, 14, 10, tzinfo=UTC)
        self.assertEqual(boundary_lag(boundary, boundary), (0.0, 0.0))

    def test_boundary_lag_one_bucket(self) -> None:
        boundary = datetime(2026, 8, 15, 14, 10, tzinfo=UTC)
        latest = datetime(2026, 8, 15, 14, 5, tzinfo=UTC)
        self.assertEqual(boundary_lag(boundary, latest), (300.0, 1.0))

    def test_legacy_change_formula_matches_scanner_formula(self) -> None:
        self.assertEqual(legacy_change_pct(100.0, 106.0), 6.0)
        self.assertEqual(legacy_change_pct(0.0, 106.0), 0.0)


if __name__ == "__main__":
    unittest.main()
