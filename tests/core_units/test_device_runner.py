"""CLI argument handling and status codes for single-device execution."""

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from seq_quince import device_runner


class DeviceRunnerTests(unittest.TestCase):
    @staticmethod
    def argv(*extra):
        return [
            "device_runner",
            "--config",
            "beamline",
            "--device",
            "mono",
            "--preset",
            "home",
            *extra,
        ]

    def test_parser_requires_all_three_selection_options(self):
        for missing in ("--config", "--device", "--preset"):
            argv = self.argv()
            index = argv.index(missing)
            del argv[index : index + 2]
            with self.subTest(missing=missing):
                with patch.object(sys, "argv", argv), redirect_stderr(
                    io.StringIO()
                ), self.assertRaises(SystemExit) as caught:
                    device_runner._parse_args()
                self.assertEqual(caught.exception.code, 2)

    def test_parser_defaults_and_logging_level(self):
        with patch.object(sys, "argv", self.argv()):
            args = device_runner._parse_args()
        self.assertEqual(
            (args.config, args.device, args.preset), ("beamline", "mono", "home")
        )
        self.assertEqual(args.replace, [])
        with patch.object(
            device_runner, "set_valid_logging_level"
        ) as set_level, patch.object(sys, "argv", self.argv("--log-level", "2")):
            device_runner._parse_args()
        set_level.assert_called_once_with(2)

    def test_invalid_logging_level_is_parser_error(self):
        with patch.object(sys, "argv", self.argv("--log-level", "6")), redirect_stderr(
            io.StringIO()
        ), self.assertRaises(SystemExit) as caught:
            device_runner._parse_args()
        self.assertEqual(caught.exception.code, 2)

    def test_main_forwards_selection_and_parsed_replacements(self):
        with patch.object(
            sys, "argv", self.argv("--replace", "IOC=TEST", "MODE=align")
        ), patch.object(device_runner, "run_device") as run_device, redirect_stdout(
            io.StringIO()
        ):
            self.assertEqual(device_runner.main(), 0)
        run_device.assert_called_once_with(
            config="beamline",
            device_name="mono",
            preset="home",
            replacements={"IOC": "TEST", "MODE": "align"},
        )

    def test_execution_errors_return_failure_status(self):
        with patch.object(sys, "argv", self.argv()), patch.object(
            device_runner, "run_device", side_effect=ValueError("unknown preset")
        ), self.assertLogs(
            "seq_quince.device_runner", level="ERROR"
        ) as logs, redirect_stdout(io.StringIO()):
            self.assertEqual(device_runner.main(), 1)
        self.assertIn("unknown preset", "\n".join(logs.output))

    def test_replacement_errors_do_not_start_device(self):
        with patch.object(sys, "argv", self.argv("--replace", "bad")), patch.object(
            device_runner, "run_device"
        ) as run_device, patch.object(
            device_runner,
            "parse_replacements",
            side_effect=ValueError("bad replacement"),
        ), self.assertLogs("seq_quince.device_runner", level="ERROR"), redirect_stdout(
            io.StringIO()
        ):
            self.assertEqual(device_runner.main(), 1)
        run_device.assert_not_called()

    def test_run_returns_main_status(self):
        with patch.object(device_runner, "main", return_value=7) as main:
            self.assertEqual(device_runner.run(), 7)
        main.assert_called_once_with()

    def test_keyboard_interrupt_returns_130(self):
        with patch.object(
            device_runner, "main", side_effect=KeyboardInterrupt()
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(device_runner.run(), 130)
