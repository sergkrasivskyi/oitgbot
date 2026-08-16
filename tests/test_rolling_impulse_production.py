from __future__ import annotations

import json
import os
import tempfile
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

from oitgbot.app import configure_scheduler
from oitgbot.models import RollingOIWindowResult
from oitgbot.scheduler_jobs import SchedulerJobs
from oitgbot.services.report_formatter import ReportFormatter
from oitgbot.services.rolling_impulse_publisher import RollingImpulsePublisher
from oitgbot.services.rolling_oi_signal_persistence import (
    RollingOISignalStatePersistence,
)
from oitgbot.services.rolling_oi_shadow_runtime import RollingOIShadowRuntime
from oitgbot.services.rolling_oi_signal_state import (
    RollingOISignalEventType,
    RollingOISignalState,
    RollingOISignalStateMachine,
)

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def result(
    value: float,
    *,
    symbol: str = "BTCUSDT",
    now: datetime = NOW,
    price: float | None = 1.0,
) -> RollingOIWindowResult:
    return RollingOIWindowResult(
        symbol=symbol,
        window_seconds=300,
        available=True,
        unavailable_reason=None,
        latest_timestamp=now,
        baseline_timestamp=now - timedelta(minutes=5),
        target_timestamp=now - timedelta(minutes=5),
        actual_window_seconds=300.0,
        baseline_offset_seconds=0.0,
        latest_oi_quantity=100 + value,
        baseline_oi_quantity=100.0,
        oi_quantity_change_pct=value,
        latest_mark_price=101.0 if price is not None else None,
        baseline_mark_price=100.0 if price is not None else None,
        price_change_pct=price,
        latest_oi_value_usd=None,
        baseline_oi_value_usd=None,
        oi_value_change_pct=None,
    )


class RecordingSender:
    def __init__(self, outcome: bool = True) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, str, str, str]] = []

    async def send_if_not_empty(
        self, chat_id: str, text: str, *, report_type: str, target_name: str
    ) -> bool:
        self.calls.append((chat_id, text, report_type, target_name))
        return self.outcome


class RollingImpulsePublishingTests(IsolatedAsyncioTestCase):
    def publisher(self, sender: RecordingSender) -> RollingImpulsePublisher:
        return RollingImpulsePublisher(
            sender,
            ReportFormatter(),
            all_channel_id="all",
            prop_channel_id="prop",
            prop_symbols={"BTCUSDT"},
        )

    async def test_positive_and_negative_triggers_publish_with_all_prop_routing(self) -> None:
        for value in (5.0, -5.0):
            with self.subTest(value=value):
                sender = RecordingSender()
                machine = RollingOISignalStateMachine()
                event = machine.evaluate(result(value))
                assert event is not None
                await self.publisher(sender).publish(event)
                self.assertEqual(["all", "prop"], [call[3] for call in sender.calls])
                self.assertTrue(all(call[2] == "impulses" for call in sender.calls))
                self.assertIn(f"{value:+.2f}", sender.calls[0][1])
                self.assertIn("BTCUSDT", sender.calls[0][1])

    async def test_non_prop_trigger_goes_only_to_all(self) -> None:
        sender = RecordingSender()
        event = RollingOISignalStateMachine().evaluate(
            result(5.0, symbol="ETHUSDT")
        )
        assert event is not None
        await self.publisher(sender).publish(event)
        self.assertEqual(["all"], [call[3] for call in sender.calls])

    async def test_missing_price_does_not_suppress_trigger(self) -> None:
        sender = RecordingSender()
        event = RollingOISignalStateMachine().evaluate(result(5.0, price=None))
        assert event is not None
        await self.publisher(sender).publish(event)
        self.assertEqual(2, len(sender.calls))
        self.assertIn("| NA |", sender.calls[0][1])

    async def test_delivery_failure_does_not_rollback_or_repeat_state(self) -> None:
        sender = RecordingSender(outcome=False)
        machine = RollingOISignalStateMachine()
        event = machine.evaluate(result(5.0))
        assert event is not None
        await self.publisher(sender).publish(event)
        self.assertEqual(RollingOISignalState.POSITIVE_TRIGGERED, machine.state_for("BTCUSDT").state)
        self.assertIsNone(machine.evaluate(result(6.0, now=NOW + timedelta(seconds=30))))
        self.assertEqual(2, len(sender.calls))

    async def test_rearm_is_not_a_publishable_trigger_and_new_crossing_is(self) -> None:
        machine = RollingOISignalStateMachine()
        trigger = machine.evaluate(result(5.0))
        rearm = machine.evaluate(result(3.0, now=NOW + timedelta(seconds=30)))
        retrigger = machine.evaluate(result(5.0, now=NOW + timedelta(seconds=60)))
        self.assertEqual(RollingOISignalEventType.TRIGGER, trigger.event_type)  # type: ignore[union-attr]
        self.assertEqual(RollingOISignalEventType.REARM, rearm.event_type)  # type: ignore[union-attr]
        self.assertEqual(RollingOISignalEventType.TRIGGER, retrigger.event_type)  # type: ignore[union-attr]

        class EventPublisher:
            def __init__(self) -> None:
                self.events = []

            async def publish(self, accepted: object) -> None:
                self.events.append(accepted)

        publisher = EventPublisher()
        runtime = object.__new__(RollingOIShadowRuntime)
        runtime.signal_publisher = publisher
        runtime._publish_tasks = set()
        runtime._schedule_trigger_publications((trigger, rearm, retrigger))  # type: ignore[arg-type]
        await asyncio.gather(*tuple(runtime._publish_tasks))
        self.assertEqual([trigger, retrigger], publisher.events)

    async def test_direct_reversals_publish_only_new_opposite_triggers(self) -> None:
        for first, opposite in ((5.0, -5.0), (-5.0, 5.0)):
            with self.subTest(first=first):
                sender = RecordingSender()
                publisher = self.publisher(sender)
                machine = RollingOISignalStateMachine()
                first_event = machine.evaluate(result(first))
                opposite_event = machine.evaluate(
                    result(opposite, now=NOW + timedelta(seconds=30))
                )
                persistent = machine.evaluate(
                    result(opposite * 1.1, now=NOW + timedelta(seconds=60))
                )
                assert first_event is not None and opposite_event is not None
                await publisher.publish(first_event)
                await publisher.publish(opposite_event)
                self.assertIsNone(persistent)
                self.assertEqual(4, len(sender.calls))
                self.assertIn(f"{opposite:+.2f}", sender.calls[-1][1])

    async def test_persistence_failure_is_logged_without_escaping(self) -> None:
        class FailingPersistence:
            path = "failure.json"

            @staticmethod
            def save(*_args: object) -> None:
                raise OSError("disk unavailable")

        runtime = object.__new__(RollingOIShadowRuntime)
        runtime.signal_state_persistence = FailingPersistence()
        runtime.signal_state_machine = RollingOISignalStateMachine()
        with self.assertLogs("oitgbot.rolling.runtime", level="ERROR") as captured:
            await runtime._persist_signal_state(NOW)
        self.assertIn("ROLLING_SIGNAL_STATE status=save_failed", captured.output[0])


class RollingSignalPersistenceTests(TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "signal.json"
        self.persistence = RollingOISignalStatePersistence(str(self.path), 15)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_recent_trigger_survives_restart_and_suppresses_duplicate(self) -> None:
        original = RollingOISignalStateMachine()
        original.evaluate(result(5.0))
        self.persistence.save(original, NOW)
        restored = RollingOISignalStateMachine()
        self.assertEqual(1, self.persistence.load(restored, NOW + timedelta(minutes=2)))
        self.assertIsNone(restored.evaluate(result(6.0, now=NOW + timedelta(minutes=5))))

    def test_recent_restore_gets_enough_ttl_grace_for_natural_warmup(self) -> None:
        original = RollingOISignalStateMachine()
        original.evaluate(result(5.0))
        self.persistence.save(original, NOW)
        restart_utc = NOW + timedelta(minutes=14)
        restored = RollingOISignalStateMachine()
        self.assertEqual(1, self.persistence.load(restored, restart_utc))
        warm_utc = restart_utc + timedelta(minutes=5)
        self.assertEqual(0, restored.expire_active(warm_utc, self.persistence.ttl))
        self.assertIsNone(restored.evaluate(result(6.0, now=warm_utc)))

    def test_stale_state_expires_safely(self) -> None:
        original = RollingOISignalStateMachine()
        original.evaluate(result(-5.0))
        self.persistence.save(original, NOW)
        restored = RollingOISignalStateMachine()
        self.assertEqual(0, self.persistence.load(restored, NOW + timedelta(minutes=16)))
        self.assertEqual(RollingOISignalState.NORMAL, restored.state_for("BTCUSDT").state)

    def test_live_confirmed_state_does_not_expire_on_persistence_ttl(self) -> None:
        machine = RollingOISignalStateMachine()
        machine.evaluate(result(5.0))
        self.assertEqual(0, machine.expire_active(NOW + timedelta(hours=1), self.persistence.ttl))
        self.assertEqual(RollingOISignalState.POSITIVE_TRIGGERED, machine.state_for("BTCUSDT").state)

    def test_missing_and_corrupt_files_fail_safe(self) -> None:
        self.assertEqual(0, self.persistence.load(RollingOISignalStateMachine(), NOW))
        self.path.write_text("not json", encoding="utf-8")
        self.assertEqual(0, self.persistence.load(RollingOISignalStateMachine(), NOW))

    def test_save_uses_atomic_replace_and_writes_valid_json(self) -> None:
        machine = RollingOISignalStateMachine()
        machine.evaluate(result(5.0))
        with patch(
            "oitgbot.services.rolling_oi_signal_persistence.os.replace",
            wraps=os.replace,
        ) as replace:
            self.persistence.save(machine, NOW)
        replace.assert_called_once()
        self.assertEqual(1, json.loads(self.path.read_text(encoding="utf-8"))["version"])
        self.assertEqual([], list(self.path.parent.glob("*.tmp")))


class ProductionSchedulerTests(TestCase):
    def test_only_rolling_top_snapshot_is_scheduled(self) -> None:
        calls: list[tuple[object, dict[str, object]]] = []
        scheduler = SimpleNamespace(
            add_job=lambda function, *args, **kwargs: calls.append((function, kwargs))
        )
        jobs = SimpleNamespace(job_top=object())
        configure_scheduler(scheduler, jobs)  # type: ignore[arg-type]
        self.assertEqual(["top_20m"], [call[1]["id"] for call in calls])
        self.assertIs(jobs.job_top, calls[0][0])
        self.assertFalse(hasattr(SchedulerJobs, "job_impulses"))
