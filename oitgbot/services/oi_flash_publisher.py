from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from .oi_flash import OIFlashCandidate
from .report_formatter import ReportFormatter

logger = logging.getLogger("oitgbot.rolling.oi_flash.publisher")


class OIFlashPublisher:
    """Publish deterministic FLASH batches to one dedicated Telegram target."""

    def __init__(
        self,
        telegram_sender: Any,
        formatter: ReportFormatter,
        *,
        chat_id: str,
        max_message_length: int = 4096,
    ) -> None:
        self.telegram_sender = telegram_sender
        self.formatter = formatter
        self.chat_id = chat_id
        self.max_message_length = max_message_length

    async def publish(self, events: Sequence[OIFlashCandidate]) -> tuple[bool, ...]:
        ordered = tuple(
            sorted(events, key=lambda item: (-abs(item.oi_pct), item.symbol))
        )
        outcomes: list[bool] = []
        chunks = self.formatter.format_oi_flash_chunks(
            ordered, max_length=self.max_message_length
        )
        for index, chunk in enumerate(chunks, 1):
            try:
                sent = await self.telegram_sender.send_if_not_empty(
                    self.chat_id,
                    self.formatter.format_oi_flash(chunk),
                    report_type="oi_flash",
                    target_name="flash",
                )
            except Exception:
                sent = False
                logger.exception(
                    "OI_FLASH_PUBLISH status=failed chunk=%d/%d",
                    index,
                    len(chunks),
                )
            outcomes.extend([sent] * len(chunk))
            logger.info(
                "OI_FLASH_PUBLISH status=%s chunk=%d/%d events=%d",
                "sent" if sent else "failed",
                index,
                len(chunks),
                len(chunk),
            )
        return tuple(outcomes)
