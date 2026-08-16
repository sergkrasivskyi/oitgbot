from __future__ import annotations

import logging
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from oitgbot import logger_setup


class LoggingRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = logging.getLogger()
        self.original_handlers = list(self.root.handlers)
        for handler in self.original_handlers:
            self.root.removeHandler(handler)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.bot_log = Path(self.tmpdir.name) / "bot.log"
        self.rolling_log = Path(self.tmpdir.name) / "rolling_oi.log"
        self.settings = replace(
            logger_setup.settings,
            log_file=str(self.bot_log),
            rolling_oi_log_file=str(self.rolling_log),
        )
        self.patch = patch.object(logger_setup, "settings", self.settings)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        for handler in list(self.root.handlers):
            self.root.removeHandler(handler)
            handler.close()
        for handler in self.original_handlers:
            self.root.addHandler(handler)
        self.tmpdir.cleanup()

    def test_routes_records_by_logger_ownership_without_duplicates(self) -> None:
        logger_setup.setup_logging()
        logging.getLogger("oitgbot.rolling.collector").info(
            "ROLLING_OI_SHADOW test=true"
        )
        logging.getLogger("oi_publisher").info("Scheduler started")
        logging.getLogger("oitgbot.rolling.price_stream").error(
            "unprefixed rolling component failure"
        )

        # Reconfiguration replaces the owned handlers instead of accumulating them.
        logger_setup.setup_logging()
        logging.getLogger("oitgbot.rolling.runtime").info("one rolling line")

        bot_text = self.bot_log.read_text(encoding="utf-8")
        rolling_text = self.rolling_log.read_text(encoding="utf-8")
        self.assertIn("Scheduler started", bot_text)
        self.assertNotIn("ROLLING_OI_SHADOW test=true", bot_text)
        self.assertNotIn("unprefixed rolling component failure", bot_text)
        self.assertNotIn("Scheduler started", rolling_text)
        self.assertIn("ROLLING_OI_SHADOW test=true", rolling_text)
        self.assertIn("unprefixed rolling component failure", rolling_text)
        self.assertEqual(rolling_text.count("one rolling line"), 1)
        self.assertRegex(bot_text, r"^\d{4}-\d{2}-\d{2} .* \| INFO \|")
        self.assertRegex(rolling_text, r"^\d{4}-\d{2}-\d{2} .* \| INFO \|")

