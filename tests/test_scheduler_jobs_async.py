from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

from oitgbot.scheduler_jobs import SchedulerJobs
from oitgbot.services.oi_scanner import OIScanResult, OIScanner


class FakeBinanceAPI:
    def get_perpetual_futures_symbols(self) -> list[str]:
        return ["BTCUSDT"]


class SlowLegacyScanner:
    def __init__(self) -> None:
        self.thread_ids: list[int] = []
        self.active = 0
        self.peak_active = 0
        self._lock = threading.Lock()

    def _scan(self, window: str) -> OIScanResult:
        self.thread_ids.append(threading.get_ident())
        with self._lock:
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
        try:
            time.sleep(0.1)
        finally:
            with self._lock:
                self.active -= 1
        now = datetime.now(timezone.utc)
        return OIScanResult(window, [], 0, [], now, now, 1)

    def scan_oi_5m_all(self, _symbols: object) -> OIScanResult:
        return self._scan("5m")

    def scan_oi_20m_all(self, _symbols: object) -> OIScanResult:
        return self._scan("20m")

    @staticmethod
    def log_scan_diagnostics(_result: OIScanResult) -> None:
        return None

    @staticmethod
    def log_top(*_args: object, **_kwargs: object) -> None:
        return None

    @staticmethod
    def log_qualifying_diagnostics(*_args: object) -> None:
        return None

    @staticmethod
    def filter_prop(rows: list[object], _symbols: set[str]) -> list[object]:
        return rows


class NoopSender:
    async def send_if_not_empty(self, *_args: object, **_kwargs: object) -> bool:
        raise AssertionError("empty legacy scan must not send Telegram")


class NoopFormatter:
    def format_message(self, *_args: object, **_kwargs: object) -> str:
        raise AssertionError("empty legacy scan must not format a report")


TEST_SETTINGS = SimpleNamespace(
    impulse_threshold=5.0,
    top_threshold=1.0,
    show_top_when_empty=False,
    top_when_empty_n=10,
    send_empty_reports=False,
    prop_symbols=set(),
    debug_oi=False,
)


class SchedulerEventLoopTests(IsolatedAsyncioTestCase):
    def make_jobs(self, scanner: SlowLegacyScanner) -> SchedulerJobs:
        return SchedulerJobs(
            binance_api=FakeBinanceAPI(),  # type: ignore[arg-type]
            telegram_sender=NoopSender(),  # type: ignore[arg-type]
            oi_scanner=scanner,  # type: ignore[arg-type]
            report_formatter=NoopFormatter(),  # type: ignore[arg-type]
        )

    async def _assert_job_does_not_starve_async_heartbeat(self, job: object) -> None:
        heartbeat_progressed = asyncio.Event()

        async def mark_price_heartbeat() -> None:
            await asyncio.sleep(0.01)
            heartbeat_progressed.set()

        heartbeat = asyncio.create_task(mark_price_heartbeat())
        legacy_job = asyncio.create_task(job())  # type: ignore[operator]
        await asyncio.wait_for(heartbeat_progressed.wait(), timeout=0.05)
        await heartbeat
        await legacy_job

    async def test_slow_impulse_scan_does_not_starve_mark_price_heartbeat(self) -> None:
        scanner = SlowLegacyScanner()
        jobs = self.make_jobs(scanner)
        self.addAsyncCleanup(jobs.close)
        main_thread = threading.get_ident()

        with patch("oitgbot.scheduler_jobs.settings", TEST_SETTINGS):
            await self._assert_job_does_not_starve_async_heartbeat(
                jobs.job_impulses
            )

        self.assertEqual(1, len(scanner.thread_ids))
        self.assertNotEqual(main_thread, scanner.thread_ids[0])

    async def test_slow_top_scan_does_not_starve_mark_price_heartbeat(self) -> None:
        scanner = SlowLegacyScanner()
        jobs = self.make_jobs(scanner)
        self.addAsyncCleanup(jobs.close)
        main_thread = threading.get_ident()

        with patch("oitgbot.scheduler_jobs.settings", TEST_SETTINGS):
            await self._assert_job_does_not_starve_async_heartbeat(jobs.job_top)

        self.assertEqual(1, len(scanner.thread_ids))
        self.assertNotEqual(main_thread, scanner.thread_ids[0])

    async def test_colliding_legacy_jobs_share_one_outer_worker(self) -> None:
        scanner = SlowLegacyScanner()
        jobs = self.make_jobs(scanner)
        self.addAsyncCleanup(jobs.close)

        with patch("oitgbot.scheduler_jobs.settings", TEST_SETTINGS):
            await asyncio.gather(jobs.job_impulses(), jobs.job_top())

        self.assertEqual(2, len(scanner.thread_ids))
        self.assertEqual(1, scanner.peak_active)


class LegacyFormulaCompatibilityTests(TestCase):
    def test_historical_oi_percentage_formula_is_unchanged(self) -> None:
        self.assertEqual(5.0, OIScanner._pct(100.0, 105.0))
        self.assertEqual(0.0, OIScanner._pct(0.0, 105.0))
