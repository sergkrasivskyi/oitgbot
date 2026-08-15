from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import settings


def setup_logging() -> logging.Logger:
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    if not root.handlers:
        file_handler = RotatingFileHandler(
            settings.log_file,
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)

        root.addHandler(file_handler)
        root.addHandler(stream_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.WARNING)

    return logging.getLogger("oi_publisher")