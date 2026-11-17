"""Single-device selection and execution through the real scheduler."""

import asyncio
import copy
import io
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import yaml

from seq_quince import runner
from seq_quince.device_concrete.sim import SimDevice
from seq_quince.scheduler import MoveError, Scheduler


def manager_for(configs=None, presets=None, replacements=None):
    """Provide configuration data without constructing device boundaries."""
    return SimpleNamespace(
        get_device_configs=Mock(
            return_value=[{"name": "mono", "type": "SimDevice"}]
            if configs is None
            else configs
        ),
        presets={"home": {"mono": {"position": 5}}} if presets is None else presets,
        replacements={} if replacements is None else replacements,
    )


def capture_final_snapshot(device):
    """Observe final state before runner-owned disposal ends device access."""
    snapshots = []
    real_close = device.close

    async def close():
        snapshots.append(await device.snapshot())
        await real_close()

    device.close = AsyncMock(side_effect=close)
    return snapshots, device.close


class DeviceSelectionTests(unittest.TestCase):
    def test_creates_only_selected_device_and_copies_its_preset(self):
        manager = manager_for(
            configs=[
                {"name": "mono", "type": "SimDevice"},
                {"name": "offline", "type": "UnavailableDevice"},
            ],
            presets={"home": {"mono": {"position": 5}, "offline": {"position": 8}}},
        )
        device = SimDevice("mono")
        with patch.object(
            runner.DeviceFactory, "create", return_value=device
        ) as create:
            step = runner.create_device_step(manager, "mono", "home")
        self.assertEqual(create.call_count, 1)
        self.assertEqual(create.call_args[0][0]["name"], "mono")
        self.assertIs(step.device, device)
        self.assertEqual(step.pose, {"position": 5})
        self.assertEqual(step.group_id, 0)

    def test_invalid_selection_fails_before_factory_creation(self):
        cases = [
            (manager_for(), "mono", "missing", "missing"),
            (manager_for(), "missing", "home", "missing"),
            (manager_for(configs=[{"name": "other"}]), "mono", "home", "mono"),
            (manager_for(presets={"home": {"other": {}}}), "mono", "home", "mono"),
            (
                manager_for(configs=[{"name": "mono"}, {"name": "mono"}]),
                "mono",
                "home",
                "mono",
            ),
        ]
        for manager, device_name, preset, expected in cases:
            with self.subTest(device=device_name, preset=preset):
                with patch.object(
                    runner.DeviceFactory, "create"
                ) as create, self.assertRaisesRegex(ValueError, expected):
                    runner.create_device_step(manager, device_name, preset)
                create.assert_not_called()

    def test_blank_names_are_rejected_before_factory_creation(self):
        for device_name, preset in (("", "home"), (" ", "home"), ("mono", "")):
            with self.subTest(device=device_name, preset=preset):
                with patch.object(
                    runner.DeviceFactory, "create"
                ) as create, self.assertRaises(ValueError):
                    runner.create_device_step(manager_for(), device_name, preset)
                create.assert_not_called()

    def test_nonmapping_preset_and_pose_fail_before_factory_creation(self):
        for presets in (
            {"home": None},
            {"home": []},
            {"home": {"mono": None}},
            {"home": {"mono": [1, 2]}},
        ):
            with self.subTest(presets=presets):
                with patch.object(
                    runner.DeviceFactory, "create"
                ) as create, self.assertRaises(TypeError):
                    runner.create_device_step(
                        manager_for(presets=presets), "mono", "home"
                    )
                create.assert_not_called()

    def test_nonmapping_presets_section_is_rejected_before_factory_creation(self):
        manager = manager_for(presets=[])
        with patch.object(runner.DeviceFactory, "create") as create, self.assertRaises(
            TypeError
        ):
            runner.create_device_step(manager, "mono", "home")
        create.assert_not_called()

    def test_empty_pose_is_valid(self):
        with patch.object(
            runner.DeviceFactory, "create", return_value=SimDevice("mono")
        ):
            step = runner.create_device_step(
                manager_for(presets={"home": {"mono": {}}}), "mono", "home"
            )
        self.assertEqual(step.pose, {})

    def test_nested_configuration_and_pose_are_independent_copies(self):
        configs = [
            {
                "name": "mono",
                "type": "SimDevice",
                "config": {"axes": ["energy"]},
                "replacements": {"IOC": "CONFIG", "LOCAL": "keep"},
            }
        ]
        presets = {"home": {"mono": {"options": {"enabled": True}, "mode": "align"}}}
        replacements = {"IOC": "CLI", "EXTRA": "value"}
        original = copy.deepcopy((configs, presets, replacements))

        def create(config):
            self.assertEqual(
                config["replacements"],
                {
                    "IOC": "CLI",
                    "EXTRA": "value",
                    "LOCAL": "keep",
                },
            )
            config["config"]["axes"].append("gap")
            return SimDevice("mono")

        with patch.object(runner.DeviceFactory, "create", side_effect=create):
            step = runner.create_device_step(
                manager_for(configs, presets, replacements), "mono", "home"
            )
        self.assertEqual(step.pose["mode"], "align")
        step.pose["options"]["enabled"] = False
        self.assertEqual((configs, presets, replacements), original)

    def test_factory_error_propagates_without_conversion(self):
        error = RuntimeError("cannot connect")
        with patch.object(
            runner.DeviceFactory, "create", side_effect=error
        ), self.assertRaises(RuntimeError) as caught:
            runner.create_device_step(manager_for(), "mono", "home")
        self.assertIs(caught.exception, error)


class DeviceExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        # Exercise the real recovery path without its production five-second
        # grace period outlasting the tests' one-second deadlock guard.
        scheduler_patch = patch.object(
            runner, "Scheduler", side_effect=partial(Scheduler, cleanup_timeout=0.05)
        )
        scheduler_patch.start()
        self.addCleanup(scheduler_patch.stop)

    async def test_async_run_moves_selected_device_through_scheduler(self):
        device = SimDevice("mono", {"position": 1}, delay=0)
        device.snapshot = AsyncMock(wraps=device.snapshot)
        final_snapshots, close = capture_final_snapshot(device)
        with patch.object(runner.DeviceFactory, "create", return_value=device):
            await runner.run_device_preset(manager_for(), "mono", "home")
        self.assertEqual(final_snapshots, [{"position": 5}])
        close.assert_awaited_once_with()
        # Rollback is off by default: only the final snapshot, no pre-run one.
        self.assertEqual(device.snapshot.await_count, 1)
        self.assertFalse(device.busy)

    async def test_async_run_with_rollback_takes_pre_run_snapshot(self):
        device = SimDevice("mono", {"position": 1}, delay=0)
        device.snapshot = AsyncMock(wraps=device.snapshot)
        device.process = AsyncMock(wraps=device.process)
        final_snapshots, _close = capture_final_snapshot(device)
        manager = manager_for(presets={"home": {"mono": {"position": 5, "rollback": True}}})
        with patch.object(runner.DeviceFactory, "create", return_value=device):
            await runner.run_device_preset(manager, "mono", "home")
        self.assertEqual(final_snapshots, [{"position": 5}])
        self.assertEqual(device.snapshot.await_count, 2)
        device.process.assert_awaited_once_with({"position": 5})

    async def test_device_timeout_is_honored(self):
        device = SimDevice("mono", delay=60)
        final_snapshots, close = capture_final_snapshot(device)
        device.scheduler_timeout = Mock(return_value=0.001)
        device.stop = AsyncMock(wraps=device.stop)
        with patch.object(
            runner.DeviceFactory, "create", return_value=device
        ), self.assertLogs("seq_quince.scheduler", level="ERROR"), self.assertRaises(
            MoveError
        ) as caught:
            await asyncio.wait_for(
                runner.run_device_preset(manager_for(), "mono", "home"), 1
            )
        self.assertIsInstance(caught.exception.__cause__, asyncio.TimeoutError)
        device.scheduler_timeout.assert_called_once_with({"position": 5})
        device.stop.assert_awaited_once_with()
        self.assertFalse(device.busy)
        self.assertEqual(final_snapshots, [{}])
        close.assert_awaited_once_with()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await device.snapshot()

    async def test_process_error_is_reported_by_scheduler(self):
        device = SimDevice("mono", delay=0)
        error = RuntimeError("motion failed")
        device.process = AsyncMock(side_effect=error)
        with patch.object(
            runner.DeviceFactory, "create", return_value=device
        ), self.assertLogs("seq_quince.scheduler", level="ERROR"), self.assertRaises(
            MoveError
        ) as caught:
            await runner.run_device_preset(manager_for(), "mono", "home")
        self.assertIs(caught.exception.__cause__, error)
        device.process.assert_awaited_once_with({"position": 5})

    async def test_cancellation_stops_selected_device_and_propagates(self):
        started = asyncio.Event()
        device = SimDevice("mono")

        async def wait_forever(_pose):
            started.set()
            await asyncio.Event().wait()

        device.process = wait_forever
        device.stop = AsyncMock()
        with patch.object(runner.DeviceFactory, "create", return_value=device):
            task = asyncio.create_task(
                runner.run_device_preset(manager_for(), "mono", "home")
            )
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        device.stop.assert_awaited_once_with()

    async def test_stop_error_does_not_mask_cancellation(self):
        device = SimDevice("mono")
        device.process = AsyncMock(side_effect=asyncio.CancelledError())
        device.stop = AsyncMock(side_effect=RuntimeError("stop failed"))
        with patch.object(
            runner.DeviceFactory, "create", return_value=device
        ), self.assertLogs("seq_quince.scheduler", level="ERROR") as logs, self.assertRaises(
            asyncio.CancelledError
        ):
            await runner.run_device_preset(manager_for(), "mono", "home")
        device.stop.assert_awaited_once_with()
        self.assertIn("Stop failed for mono: stop failed", "\n".join(logs.output))

    async def test_cancellation_waits_for_scheduler_stop_and_process_settlement(self):
        started = asyncio.Event()
        stopping = asyncio.Event()
        allow_stop = asyncio.Event()
        stopped = asyncio.Event()
        settled = asyncio.Event()
        device = SimDevice("mono")

        async def process(_pose):
            started.set()
            try:
                await stopped.wait()
            finally:
                settled.set()

        async def stop():
            stopping.set()
            await allow_stop.wait()
            stopped.set()
            await settled.wait()

        device.process = process
        device.stop = AsyncMock(side_effect=stop)
        with patch.object(runner.DeviceFactory, "create", return_value=device):
            task = asyncio.create_task(
                runner.run_device_preset(manager_for(), "mono", "home")
            )
            try:
                await asyncio.wait_for(started.wait(), 1)
                task.cancel()
                await asyncio.wait_for(stopping.wait(), 1)
                self.assertFalse(task.done())
                self.assertFalse(settled.is_set())
                task.cancel()  # Repeated cancellation must preserve cleanup.
                allow_stop.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
                self.assertTrue(settled.is_set())
            finally:
                allow_stop.set()
                stopped.set()
                await asyncio.gather(task, return_exceptions=True)
        device.stop.assert_awaited_once_with()

    async def test_caller_timeout_during_recovery_remains_timeout(self):
        stopping = asyncio.Event()
        device = SimDevice("mono", delay=60)
        final_snapshots, close = capture_final_snapshot(device)
        device.scheduler_timeout = Mock(return_value=0.001)
        real_stop = device.stop

        async def stop():
            await real_stop()
            stopping.set()

        device.stop = AsyncMock(side_effect=stop)
        with patch.object(
            runner.DeviceFactory, "create", return_value=device
        ), self.assertLogs("seq_quince.scheduler", level="ERROR"):
            task = asyncio.create_task(
                runner.run_device_preset(manager_for(), "mono", "home")
            )
            try:
                await asyncio.wait_for(stopping.wait(), 1)
                # Start this caller deadline only once recovery has begun;
                # it expires before the 0.05-second grace period finishes.
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(task, 0.01)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        device.stop.assert_awaited_once_with()
        self.assertFalse(device.busy)
        self.assertEqual(final_snapshots, [{}])
        close.assert_awaited_once_with()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await device.snapshot()


class SynchronousDeviceTests(unittest.TestCase):
    def test_wrapper_constructs_device_in_worker_and_executes_in_its_loop(self):
        device = SimDevice("mono", delay=0)
        final_snapshots, close = capture_final_snapshot(device)
        created_on = []
        caller_thread = threading.get_ident()

        def create(_config):
            created_on.append(threading.get_ident())
            return device

        manager = manager_for()
        replacements = {"IOC": "TEST"}
        with patch.object(
            runner, "create_config_manager", return_value=manager
        ) as load, patch.object(runner.DeviceFactory, "create", side_effect=create):
            self.assertIsNone(
                runner.run_device("beamline", "mono", "home", replacements=replacements)
            )
        load.assert_called_once_with("beamline", replacements)
        self.assertEqual(len(created_on), 1)
        self.assertNotEqual(created_on[0], caller_thread)
        self.assertEqual(final_snapshots, [{"position": 5}])
        close.assert_awaited_once_with()

    def test_real_yaml_selects_only_one_device_and_user_preset_override(self):
        config = {
            "devices": [
                {
                    "name": "mono",
                    "type": "SimDevice",
                    "delay": 0,
                    "initial_pose": {"position": 1},
                },
                {"name": "offline", "type": "UnavailableDevice"},
            ],
            "presets": {"home": {"mono": {"position": 5}, "offline": {"position": 9}}},
        }
        created = []
        final_snapshots = []
        closes = []
        real_create = runner.DeviceFactory.create

        def create(raw_config):
            device = real_create(raw_config)
            created.append(device)
            snapshots, close = capture_final_snapshot(device)
            final_snapshots.append(snapshots)
            closes.append(close)
            return device

        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            (config_dir / "beamline.yaml").write_text(
                yaml.safe_dump(config), encoding="utf-8"
            )
            (config_dir / "presets.yaml").write_text(
                yaml.safe_dump({"presets": {"home": {"mono": {"position": 7}}}}),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ, {"SCHEDULER_CONFIG_DIR": directory}
            ), patch.object(
                runner.DeviceFactory, "create", side_effect=create
            ), redirect_stdout(io.StringIO()):
                runner.run_device("ignored", "mono", "home")
        self.assertEqual(len(created), 1)
        self.assertEqual(final_snapshots, [[{"position": 7}]])
        closes[0].assert_awaited_once_with()
