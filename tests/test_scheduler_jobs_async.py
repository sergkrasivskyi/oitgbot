from __future__ import annotations

import inspect
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

from oitgbot.app import configure_scheduler, main_async
from oitgbot.models import RollingOISample
from oitgbot.scheduler_jobs import SchedulerJobs
from oitgbot.services.report_formatter import ReportFormatter
from oitgbot.services.rolling_oi_shadow_runtime import RollingOIShadowRuntime

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


class NetworkForbiddenAPI:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"TOP job attempted Binance access: {name}")


class RecordingSender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    async def send_if_not_empty(
        self, chat_id: str, text: str, *, report_type: str, target_name: str
    ) -> bool:
        self.calls.append((chat_id, text, report_type, target_name))
        return bool(text.strip())


def make_runtime() -> RollingOIShadowRuntime:
    return RollingOIShadowRuntime(
        NetworkForbiddenAPI(),
        lambda: (),
        clock=lambda: NOW,
        stream_factory=lambda _store: object(),
    )


def add_window(
    runtime: RollingOIShadowRuntime,
    symbol: str,
    change_pct: float,
    *,
    baseline_price: float | None = 100.0,
    latest_price: float | None = 101.0,
) -> None:
    runtime.rolling_store.add(
        RollingOISample(
            symbol,
            100.0,
            NOW - timedelta(minutes=20),
            NOW - timedelta(minutes=20),
            mark_price=baseline_price,
            price_exchange_time=(
                NOW - timedelta(minutes=20)
                if baseline_price is not None
                else None
            ),
        )
    )
    runtime.rolling_store.add(
        RollingOISample(
            symbol,
            100.0 + change_pct,
            NOW,
            NOW,
            mark_price=latest_price,
            price_exchange_time=NOW if latest_price is not None else None,
        )
    )


def top_settings(*, send_empty: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        top_threshold=1.0,
        send_empty_reports=send_empty,
        prop_symbols={"BOUNDARYUSDT"},
        all_channel_id="all",
        prop_channel_id="prop",
    )


class RollingTopProductionTests(IsolatedAsyncioTestCase):
    def make_jobs(
        self, runtime: RollingOIShadowRuntime, sender: RecordingSender
    ) -> SchedulerJobs:
        return SchedulerJobs(
            binance_api=NetworkForbiddenAPI(),  # type: ignore[arg-type]
            telegram_sender=sender,  # type: ignore[arg-type]
            report_formatter=ReportFormatter(),
            shadow_runtime=runtime,
        )

    async def test_quantity_threshold_sorting_price_and_all_prop_routing(self) -> None:
        runtime = make_runtime()
        add_window(runtime, "BOUNDARYUSDT", 1.0, latest_price=102.0)
        add_window(runtime, "HIGHUSDT", 3.0, latest_price=99.0)
        add_window(runtime, "LOWUSDT", 0.99)
        sender = RecordingSender()
        jobs = self.make_jobs(runtime, sender)

        with patch("oitgbot.scheduler_jobs.settings", top_settings()):
            await jobs.job_top()

        self.assertEqual(["all", "prop"], [call[3] for call in sender.calls])
        all_message = sender.calls[0][1]
        self.assertLess(all_message.index("HIGHUSDT"), all_message.index("BOUNDARYUSDT"))
        self.assertNotIn("LOWUSDT", all_message)
        self.assertIn("+3.00 | -1.00 |", all_message)
        self.assertIn("+1.00 | +2.00 |", all_message)
        self.assertIn("BOUNDARYUSDT", sender.calls[1][1])
        self.assertNotIn("HIGHUSDT", sender.calls[1][1])

    async def test_missing_price_keeps_valid_quantity_candidate(self) -> None:
        runtime = make_runtime()
        add_window(
            runtime,
            "NOPRICEUSDT",
            2.0,
            baseline_price=None,
            latest_price=None,
        )
        sender = RecordingSender()
        with patch("oitgbot.scheduler_jobs.settings", top_settings()):
            await self.make_jobs(runtime, sender).job_top()
        self.assertIn("+2.00 | NA |", sender.calls[0][1])

    async def test_warmup_skips_without_network_or_fake_empty_report(self) -> None:
        runtime = make_runtime()
        runtime.rolling_store.add(
            RollingOISample("BTCUSDT", 100.0, NOW, NOW)
        )
        sender = RecordingSender()
        with (
            patch("oitgbot.scheduler_jobs.settings", top_settings(send_empty=True)),
            self.assertLogs("oitgbot.rolling.top", level="INFO") as captured,
        ):
            await self.make_jobs(runtime, sender).job_top()
        self.assertEqual([], sender.calls)
        self.assertIn("ROLLING_TOP_SKIP reason=warmup", "\n".join(captured.output))

    async def test_send_empty_remains_compatible_after_warmup(self) -> None:
        runtime = make_runtime()
        add_window(runtime, "FLATUSDT", 0.5)
        sender = RecordingSender()
        with patch("oitgbot.scheduler_jobs.settings", top_settings(send_empty=True)):
            await self.make_jobs(runtime, sender).job_top()
        self.assertEqual(["all", "prop"], [call[3] for call in sender.calls])
        self.assertTrue(all("(no growth) rolling OI_20m" in call[1] for call in sender.calls))

    async def test_snapshot_calculation_runs_off_event_loop_without_network(self) -> None:
        runtime = make_runtime()
        add_window(runtime, "BTCUSDT", 2.0)
        calling_thread = threading.get_ident()
        snapshot_thread: list[int] = []
        original = runtime.rolling_top_snapshot

        def snapshot() -> object:
            snapshot_thread.append(threading.get_ident())
            return original()

        runtime.rolling_top_snapshot = snapshot  # type: ignore[method-assign]
        with patch("oitgbot.scheduler_jobs.settings", top_settings()):
            await self.make_jobs(runtime, RecordingSender()).job_top()
        self.assertNotEqual(calling_thread, snapshot_thread[0])

    async def test_runtime_unavailable_cannot_fall_back_to_historical_top(self) -> None:
        sender = RecordingSender()
        jobs = SchedulerJobs(
            binance_api=NetworkForbiddenAPI(),  # type: ignore[arg-type]
            telegram_sender=sender,  # type: ignore[arg-type]
            report_formatter=ReportFormatter(),
        )
        with patch("oitgbot.scheduler_jobs.settings", top_settings(send_empty=True)):
            await jobs.job_top()
        self.assertEqual([], sender.calls)


class ProductionArchitectureTests(TestCase):
    def test_schedule_remains_00_20_40_at_second_10_and_only_top(self) -> None:
        calls: list[tuple[object, object, dict[str, object]]] = []
        scheduler = SimpleNamespace(
            add_job=lambda function, trigger, **kwargs: calls.append(
                (function, trigger, kwargs)
            )
        )
        jobs = SimpleNamespace(job_top=object())
        configure_scheduler(scheduler, jobs)  # type: ignore[arg-type]
        self.assertEqual(1, len(calls))
        self.assertEqual("top_20m", calls[0][2]["id"])
        trigger_text = str(calls[0][1])
        self.assertIn("minute='0,20,40'", trigger_text)
        self.assertIn("second='10'", trigger_text)

    def test_production_top_has_no_historical_or_current_oi_scan_wiring(self) -> None:
        source = inspect.getsource(SchedulerJobs.job_top)
        self.assertNotIn("scan_oi_20m", source)
        self.assertNotIn("current_open_interest", source)
        self.assertNotIn("price_change_20m", source)
        self.assertNotIn("binance_api", source)
        startup_source = inspect.getsource(main_async)
        self.assertNotIn("OIScanner", startup_source)
        self.assertNotIn("scan_oi_20m", startup_source)

    def test_60m_and_120m_have_no_production_schedule(self) -> None:
        source = inspect.getsource(configure_scheduler)
        self.assertNotIn("60m", source)
        self.assertNotIn("120m", source)
