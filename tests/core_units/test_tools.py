"""Logging configuration, directory lookup, and replacement precedence."""

import contextlib
import io
import logging
import os
from unittest.mock import patch

from seq_quince import tools

from ._support import CoreTestCase


class ToolsTests(CoreTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(logging.getLogger().setLevel, logging.getLogger().level)

    def test_valid_logging_levels(self):
        for level in (
            logging.DEBUG,
            logging.INFO,
            logging.WARNING,
            logging.ERROR,
            logging.CRITICAL,
        ):
            with self.subTest(level=level):
                self.assertTrue(tools.is_valid_logging_level(level))
        for level in (None, "10", 0, -1, 15, 60):
            with self.subTest(level=level):
                self.assertFalse(tools.is_valid_logging_level(level))

    def test_mbbo_values_and_numeric_strings_set_root_level(self):
        for raw, expected in (
            (1, logging.DEBUG),
            (" 2 ", logging.INFO),
            (3, logging.WARNING),
            (4, logging.ERROR),
            (5, logging.CRITICAL),
        ):
            with self.subTest(raw=raw):
                tools.set_valid_logging_level(raw)
                self.assertEqual(logging.getLogger().level, expected)

    def test_invalid_mbbo_values_leave_logging_level_unchanged(self):
        logging.getLogger().setLevel(logging.WARNING)
        for raw in (0, -1, 6):
            with self.subTest(raw=raw), self.assertLogs(
                tools.logger, level="ERROR"
            ) as logs:
                tools.set_valid_logging_level(raw)
                self.assertEqual(logging.getLogger().level, logging.WARNING)
                self.assertIn("Invalid logging level", "\n".join(logs.output))
        for raw in (None, "invalid"):
            with self.subTest(raw=raw):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    tools.set_valid_logging_level(raw)
                self.assertIn("not an integer", output.getvalue())
                self.assertEqual(logging.getLogger().level, logging.WARNING)

    def test_seq_quince_config_dir_requires_an_existing_directory(self):
        file_path = self.temp_dir / "file.txt"
        file_path.write_text("file", encoding="utf-8")
        for value in (None, "", str(self.temp_dir / "missing"), str(file_path)):
            with self.subTest(value=value), patch.dict(os.environ, {}, clear=True):
                if value is not None:
                    os.environ["SEQ_QUINCE_CONFIG_DIR"] = value
                self.assertIsNone(tools.get_seq_quince_config_dir())
        relative = os.path.relpath(str(self.temp_dir))
        with patch.dict(os.environ, {"SEQ_QUINCE_CONFIG_DIR": relative}):
            self.assertEqual(
                tools.get_seq_quince_config_dir(), str(self.temp_dir.resolve())
            )

    def test_cli_replacements_override_existing_values_in_place(self):
        existing = {"IOC": "OLD", "KEEP": "yes"}
        config = {"replacements": existing, "name": "device"}
        cli = {"IOC": "NEW", "ADD": "extra"}
        tools.merge_replacements(config, cli)
        self.assertIs(config["replacements"], existing)
        self.assertEqual(
            config,
            {
                "replacements": {"IOC": "NEW", "KEEP": "yes", "ADD": "extra"},
                "name": "device",
            },
        )
        self.assertEqual(cli, {"IOC": "NEW", "ADD": "extra"})

    def test_absent_or_invalid_replacement_sections_are_copied(self):
        for config in (
            {},
            {"replacements": None},
            {"replacements": []},
            {"replacements": "bad"},
        ):
            with self.subTest(config=config):
                cli = {"IOC": "TEST"}
                tools.merge_replacements(config, cli)
                self.assertEqual(config["replacements"], {"IOC": "TEST"})
                self.assertIsNot(config["replacements"], cli)
                cli["IOC"] = "CHANGED"
                self.assertEqual(config["replacements"]["IOC"], "TEST")

    def test_no_cli_replacements_does_not_modify_config(self):
        config = {"replacements": {"IOC": "TEST"}, "name": "device"}
        before = {"replacements": {"IOC": "TEST"}, "name": "device"}
        tools.merge_replacements(config, None)
        self.assertEqual(config, before)
        absent = {}
        tools.merge_replacements(absent, None)
        self.assertEqual(absent, {})

    def test_scheduler_config_dir_uses_the_existing_environment_helper(self):
        """Resolve the scheduler directory from the environment."""
        with patch.dict(os.environ, {"SEQ_QUINCE_CONFIG_DIR": str(self.temp_dir)}):
            self.assertEqual(
                tools.get_scheduler_config_dir("line"),
                os.path.join(
                    str(self.temp_dir), "scheduler_config", "beamlines", "line"
                ),
            )
