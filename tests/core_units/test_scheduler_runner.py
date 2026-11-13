"""CLI argument handling and exit codes; execution belongs to runner.py."""

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

from seq_quince import scheduler_runner as cli


class SchedulerRunnerTests(unittest.TestCase):
    @staticmethod
    def argv(*extra):
        return ["scheduler_runner", "--config", "beamline", "--preset", "home", *extra]

    def test_parser_defaults_finite_and_infinite_loop(self):
        cases = [
            ([], 1, 0.0),
            (["--loop", "3", "--delay", "0.25"], 3, 0.25),
            (["--loop"], None, 0.0),
        ]
        for extra, expected_count, expected_delay in cases:
            with self.subTest(extra=extra), patch.object(sys, "argv", self.argv(*extra)):
                args, count = cli._parse_args()
                self.assertEqual(count, expected_count)
                self.assertEqual(args.delay, expected_delay)
                self.assertEqual((args.config, args.preset), ("beamline", "home"))
                self.assertEqual(args.replace, [])
                self.assertFalse(args.snapshot)

    def test_invalid_options_fail_before_loading_configuration(self):
        invalid = [
            ["scheduler_runner"],
            self.argv("--loop", "0"),
            self.argv("--loop", "bad"),
            self.argv("--snapshot", "--loop", "2"),
            self.argv("--snapshot", "--loop"),
            self.argv("--delay", "nan"),
            self.argv("--delay", "-1"),
            self.argv("--log-level", "6"),
        ]
        with patch.object(cli, "run_scheduler") as execute:
            for argv in invalid:
                with self.subTest(argv=argv), patch.object(
                    sys, "argv", argv
                ), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    cli.main()
                self.assertEqual(caught.exception.code, 2)
            execute.assert_not_called()

    def test_main_forwards_replacements_and_execution_options(self):
        with patch.object(
            sys,
            "argv",
            self.argv(
                "--loop", "2", "--delay", "0.25", "--replace", "IOC=TEST", "EXTRA=value"
            ),
        ), patch.object(cli, "run_scheduler") as execute:
            self.assertEqual(cli.main(), 0)
        execute.assert_called_once_with(
            config="beamline",
            preset="home",
            replacements={"IOC": "TEST", "EXTRA": "value"},
            snapshot=False,
            loop_count=2,
            delay=0.25,
            on_iteration_start=cli._print_iteration_start,
            on_iteration_end=cli._print_iteration_end,
        )

    def test_snapshot_mode_reports_success(self):
        output = io.StringIO()
        with patch.object(sys, "argv", self.argv("--snapshot")), patch.object(
            cli, "run_scheduler"
        ) as execute, redirect_stdout(output):
            self.assertEqual(cli.main(), 0)
        self.assertTrue(execute.call_args.kwargs["snapshot"])
        self.assertIn("Taking snapshot", output.getvalue())
        self.assertIn("✔", output.getvalue())

    def test_execution_error_reports_failure(self):
        for snapshot in (False, True):
            with self.subTest(snapshot=snapshot), patch.object(
                sys, "argv", self.argv(*(["--snapshot"] if snapshot else []))
            ), patch.object(
                cli, "run_scheduler", side_effect=RuntimeError("offline")
            ), redirect_stdout(io.StringIO()), self.assertLogs(cli.logger, "ERROR") as logs:
                self.assertEqual(cli.main(), 1)
            operation = "Snapshot" if snapshot else "Scheduler"
            self.assertIn(operation + " failed: offline", "\n".join(logs.output))

    def test_invalid_replacement_is_reported_without_execution(self):
        with patch.object(sys, "argv", self.argv("--replace", "invalid")), patch.object(
            cli, "parse_replacements", side_effect=ValueError("bad replacement")
        ), patch.object(cli, "run_scheduler") as execute, redirect_stdout(
            io.StringIO()
        ), self.assertLogs(cli.logger, "ERROR"):
            self.assertEqual(cli.main(), 1)
        execute.assert_not_called()

    def test_log_level_uses_shared_logging_helper(self):
        with patch.object(sys, "argv", self.argv("--log-level", "2")), patch.object(
            cli, "set_valid_logging_level"
        ) as set_level:
            cli._parse_args()
        set_level.assert_called_once_with(2)

    def test_run_preserves_status_and_handles_keyboard_interrupt(self):
        with patch.object(cli, "main", Mock(return_value=7)):
            self.assertEqual(cli.run(), 7)
        output = io.StringIO()
        with patch.object(cli, "main", side_effect=KeyboardInterrupt()), redirect_stdout(output):
            self.assertEqual(cli.run(), 130)
        self.assertIn("interrupted", output.getvalue())

    def test_iteration_callbacks_display_counts_and_completion(self):
        output = io.StringIO()
        with redirect_stdout(output):
            cli._print_iteration_start(1, 2)
            cli._print_iteration_end(1, 2)
            cli._print_iteration_start(3, None)
        self.assertIn("[1/2]", output.getvalue())
        self.assertIn("[3/∞]", output.getvalue())
        self.assertIn("✔", output.getvalue())
