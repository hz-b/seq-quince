"""Reusable configuration, scheduling, snapshot and repetition operations."""

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from seq_quince import runner


class RunnerTests(unittest.TestCase):
    def test_loop_count_normalization_and_invalid_values(self):
        for raw, expected in [
            (1, 1),
            (" 002 ", 2),
            ("infinite", None),
            (" INF ", None),
            ("Forever", None),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(runner.parse_loop_count(raw), expected)
        for raw in ("0", "-2", "bad", "3.5", "", None):
            with self.subTest(raw=raw), self.assertRaisesRegex(
                ValueError, "positive integer"
            ):
                runner.parse_loop_count(raw)

    def test_invalid_run_options_fail_before_loading_configuration(self):
        invalid = [
            (0, 0.0, False),
            (True, 0.0, False),
            (1.5, 0.0, False),
            (1, -1, False),
            (1, float("nan"), False),
            (1, float("inf"), False),
            (1, True, False),
            (1, "0", False),
            (2, 0.0, True),
            (None, 0.0, True),
        ]
        with patch.object(runner, "create_config_manager") as load:
            for count, delay, snapshot in invalid:
                with self.subTest(count=count, delay=delay, snapshot=snapshot), self.assertRaises(
                    ValueError
                ):
                    runner.run_scheduler(
                        "beamline",
                        "home",
                        loop_count=count,
                        delay=delay,
                        snapshot=snapshot,
                    )
            load.assert_not_called()

    def test_config_directory_precedence_and_replacements(self):
        replacements = {"IOC": "TEST"}
        with patch.dict(os.environ, {}, clear=True), patch.object(
            runner, "get_scheduler_config_dir", return_value="/fallback"
        ) as resolve, patch.object(runner, "SchedulerConfigManager") as manager:
            self.assertIs(
                runner.create_config_manager("beamline", replacements),
                manager.return_value,
            )
            resolve.assert_called_once_with("beamline")
            manager.assert_called_once_with(
                config_dir="/fallback",
                replacements=replacements,
                config_file="beamline.yaml",
            )
        with patch.dict(
            os.environ, {"SCHEDULER_CONFIG_DIR": "/site/config"}
        ), patch.object(runner, "get_scheduler_config_dir") as resolve, patch.object(
            runner, "SchedulerConfigManager"
        ) as manager:
            runner.create_config_manager("beamline")
            resolve.assert_not_called()
            manager.assert_called_once_with(
                config_dir="/site/config",
                replacements=None,
                config_file="beamline.yaml",
            )

    def test_scheduler_creates_devices_and_builds_serial_steps(self):
        manager = Mock()
        devices, steps = ({"mono": object()}, [object()])
        manager.create_devices.return_value = devices
        manager.build_sequence_from_preset.return_value = steps
        with patch.object(runner, "Scheduler") as constructor:
            self.assertIs(
                runner.create_scheduler(manager, "home"), constructor.return_value
            )
        manager.create_devices.assert_called_once_with(runner.DeviceFactory)
        manager.build_sequence_from_preset.assert_called_once_with(
            "home", devices, default_grouping="serial"
        )
        constructor.assert_called_once_with(steps, step_timeout=None)

    def test_device_creation_failure_propagates_before_sequence_building(self):
        manager = Mock()
        error = ValueError("bad device")
        manager.create_devices.side_effect = error
        with self.assertRaises(ValueError) as caught:
            runner.create_scheduler(manager, "home")
        self.assertIs(caught.exception, error)
        manager.build_sequence_from_preset.assert_not_called()

    def test_sequence_runs_once_and_snapshot_saves_without_motion(self):
        manager = Mock()
        snapshot = {"mono": {"position": 2}}
        scheduler = Mock(run=AsyncMock(), snapshot=AsyncMock(return_value=snapshot))
        with patch.object(runner, "create_scheduler", return_value=scheduler) as create:
            asyncio.run(runner.run_sequence(manager, "home"))
            self.assertIs(asyncio.run(runner.take_snapshot(manager, "saved")), snapshot)
        self.assertEqual(
            create.call_args_list, [call(manager, "home"), call(manager, "saved")]
        )
        scheduler.run.assert_awaited_once_with()
        scheduler.snapshot.assert_awaited_once_with()
        manager.save_snapshot.assert_called_once_with("saved", snapshot)

    def test_finite_runs_callback_order_and_delay_after_final_iteration(self):
        manager = Mock()
        events = []

        async def sequence(_manager, preset):
            events.append(("run", preset))

        async def delay(seconds):
            events.append(("delay", seconds))

        def start(iteration, count):
            events.append(("start", iteration, count))

        def end(iteration, count):
            events.append(("end", iteration, count))

        with patch.object(runner, "run_sequence", side_effect=sequence) as execute, patch.object(
            runner.asyncio, "sleep", side_effect=delay
        ):
            asyncio.run(
                runner.run_sequences(
                    manager,
                    "home",
                    loop_count=2,
                    delay=0.25,
                    on_iteration_start=start,
                    on_iteration_end=end,
                )
            )
        self.assertEqual(execute.await_count, 2)
        self.assertEqual(
            events,
            [
                ("start", 1, 2),
                ("run", "home"),
                ("end", 1, 2),
                ("delay", 0.25),
                ("start", 2, 2),
                ("run", "home"),
                ("end", 2, 2),
                ("delay", 0.25),
            ],
        )

    def test_failure_stops_repetition_without_end_callback_or_delay(self):
        end = Mock()
        error = RuntimeError("motion failed")
        with patch.object(runner, "run_sequence", side_effect=error) as execute, patch.object(
            runner.asyncio, "sleep", new_callable=AsyncMock
        ) as delay, self.assertRaises(RuntimeError) as caught:
            asyncio.run(
                runner.run_sequences(
                    Mock(), "home", loop_count=3, on_iteration_end=end
                )
            )
        self.assertIs(caught.exception, error)
        execute.assert_awaited_once()
        end.assert_not_called()
        delay.assert_not_awaited()

    def test_infinite_runs_continue_until_cancellation(self):
        start = Mock()
        with patch.object(runner, "run_sequence", new_callable=AsyncMock) as execute, patch.object(
            runner.asyncio, "sleep", side_effect=[None, asyncio.CancelledError()]
        ), self.assertRaises(asyncio.CancelledError):
            asyncio.run(
                runner.run_sequences(
                    Mock(), "home", loop_count=None, on_iteration_start=start
                )
            )
        self.assertEqual(execute.await_count, 2)
        self.assertEqual(start.call_args_list, [call(1, None), call(2, None)])

    def test_sync_wrapper_loads_once_and_forwards_loop_options(self):
        manager, start, end = (Mock(), Mock(), Mock())
        replacements = {"IOC": "TEST"}
        with patch.object(
            runner, "create_config_manager", return_value=manager
        ) as load, patch.object(
            runner, "run_sequences", new_callable=AsyncMock
        ) as execute:
            self.assertIsNone(
                runner.run_scheduler(
                    "beamline",
                    "home",
                    replacements=replacements,
                    loop_count=2,
                    delay=0.25,
                    on_iteration_start=start,
                    on_iteration_end=end,
                )
            )
        load.assert_called_once_with("beamline", replacements)
        execute.assert_awaited_once_with(
            manager,
            "home",
            loop_count=2,
            delay=0.25,
            on_iteration_start=start,
            on_iteration_end=end,
        )

    def test_sync_snapshot_returns_saved_mapping(self):
        manager, snapshot = (Mock(), {"mono": {"position": 2}})
        with patch.object(runner, "create_config_manager", return_value=manager), patch.object(
            runner, "take_snapshot", new_callable=AsyncMock, return_value=snapshot
        ) as take, patch.object(
            runner, "run_sequences", new_callable=AsyncMock
        ) as execute:
            self.assertIs(
                runner.run_scheduler("beamline", "home", snapshot=True),
                snapshot,
            )
        take.assert_awaited_once_with(manager, "home")
        execute.assert_not_awaited()
