from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

from .report_formatter import ReportFormatter
from .rolling_oi_signal_state import RollingOISignalEvent

logger = logging.getLogger("oitgbot.rolling.signal_publisher")


class RollingImpulsePublisher:
    def __init__(
        self,
        telegram_sender: Any,
        report_formatter: ReportFormatter,
        *,
        all_channel_id: str,
        prop_channel_id: str,
        prop_symbols: Iterable[str],
    ) -> None:
        self.telegram_sender = telegram_sender
        self.report_formatter = report_formatter
        self.all_channel_id = all_channel_id
        self.prop_channel_id = prop_channel_id
        self.prop_symbols = {symbol.upper() for symbol in prop_symbols}

    async def publish(self, event: RollingOISignalEvent) -> tuple[bool, bool]:
        message = self.report_formatter.format_rolling_impulse(event)

        async def send(chat_id: str, target: str) -> bool:
            try:
                return await self.telegram_sender.send_if_not_empty(
                    chat_id,
                    message,
                    report_type="impulses",
                    target_name=target,
                )
            except Exception:
                logger.exception(
                    "ROLLING_SIGNAL_PUBLISH status=failed symbol=%s target=%s",
                    event.symbol,
                    target,
                )
                return False

        all_task = asyncio.create_task(send(self.all_channel_id, "all"))
        prop_task = (
            asyncio.create_task(send(self.prop_channel_id, "prop"))
            if event.symbol.upper() in self.prop_symbols
            else None
        )
        sent_all = await all_task
        sent_prop = await prop_task if prop_task is not None else False
        succeeded = sent_all and (prop_task is None or sent_prop)
        logger.info(
            "ROLLING_SIGNAL_PUBLISH status=%s event=TRIGGER direction=%s symbol=%s all_sent=%s "
            "prop_eligible=%s prop_sent=%s",
            "complete" if succeeded else "failed",
            event.direction.value,
            event.symbol,
            sent_all,
            prop_task is not None,
            sent_prop,
        )
        return sent_all, sent_prop
