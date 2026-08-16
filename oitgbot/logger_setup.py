from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import settings

ROLLING_LOGGER_NAMESPACE = "oitgbot.rolling"


class _RollingLogFilter(logging.Filter):
    def __init__(self, include_rolling: bool) -> None:
        super().__init__()
        self.include_rolling = include_rolling

    def filter(self, record: logging.LogRecord) -> bool:
        is_rolling = record.name == ROLLING_LOGGER_NAMESPACE or record.name.startswith(
            f"{ROLLING_LOGGER_NAMESPACE}."
        )
        return is_rolling if self.include_rolling else not is_rolling


def _file_handler(path: str, formatter: logging.Formatter, *, rolling: bool) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        path,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    handler.addFilter(_RollingLogFilter(rolling))
    handler._oitgbot_log_role = "rolling" if rolling else "bot"  # type: ignore[attr-defined]
    return handler


def setup_logging() -> logging.Logger:
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    configured_handlers = [
        handler
        for handler in root.handlers
        if getattr(handler, "_oitgbot_log_role", None) is not None
    ]
    for handler in configured_handlers:
        root.removeHandler(handler)
        handler.close()

    if not root.handlers:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    root.addHandler(_file_handler(settings.log_file, formatter, rolling=False))
    root.addHandler(_file_handler(settings.rolling_oi_log_file, formatter, rolling=True))

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.WARNING)

    return logging.getLogger("oi_publisher")
