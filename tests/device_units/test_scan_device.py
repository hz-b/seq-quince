import asyncio
import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from seq_quince.device.lifecycle import DeviceScope
from seq_quince.device_concrete import scan
from seq_quince.device_concrete.sim import SimDevice
from seq_quince.scheduler import MoveError, Scheduler
from seq_quince.sequence import MoveStep

from ._support import DeviceTestCase


class ScanDeviceTests(DeviceTestCase):
    def setUp(self):
        super().setUp()
        self.patch(scan, "get_scan_config_dir", lambda: "/default/config")
        self.loader = self.patch(
            scan,
            "load_scan_config_from_file",
            Mock(
                return_value={
                    "actuators": {"energy": {"pv": "TEST:ENERGY"}},
                }
            ),
        )
        self.factory = self.patch(
            scan,
            "create_scan_with_config",
            Mock(
                side_effect=lambda *args: SimpleNamespace(
                    execute=Mock(), busy=False, stop=Mock(), close=Mock()
                )
            ),
        )
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("SCAN_CONFIG_DIR", None)
        os.environ.pop("SCAN_DATA_DIR", None)

    @staticmethod
    def make_device(**overrides):
        return scan.ScanDevice.from_config_dict(
            dict(
                {"name": "scan", "scantype": "linear", "config": "mono.yaml"},
                **overrides,
            )
        )

    def test_defaults_and_environment_paths(self):
        device = self.make_device()
        self.loader.assert_called_once_with("/default/config", "mono.yaml", {})
        self.assertEqual(
            self.run_async(device.snapshot()),
            {"start": 300, "stop": 400, "steps": 3, "velocity": 0},
        )
        self.assertIsNone(self.factory.call_args.args[2])
        os.environ["SCAN_CONFIG_DIR"] = "/site/config"
        os.environ["SCAN_DATA_DIR"] = "/site/data"
        replacements = {"IOC": "TEST"}
        device = self.make_device(
            replacements=replacements, start="1", stop="2", steps="4", velocity="0.5"
        )
        self.loader.assert_called_with("/site/config", "mono.yaml", replacements)
        self.assertEqual(self.factory.call_args.args[2], "/site/data")
        self.assertEqual(
            self.run_async(device.snapshot()),
            {"start": 1, "stop": 2, "steps": 4, "velocity": 0.5},
        )
        self.make_device(data_dir="/explicit/data")
        self.assertEqual(self.factory.call_args.args[2], "/explicit/data")

    def test_executes_sync_and_async_scans_without_rebuilding(self):
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.factory.reset_mock()
                device = self.make_device()
                instance = device.get_capability("scan")
                execute_mock = AsyncMock() if asynchronous else Mock()
                if asynchronous:
                    # Python 3.8 inspect.iscoroutinefunction needs a real async def.
                    async def execute(mock=execute_mock):
                        await mock()

                    instance.execute = execute
                else:
                    instance.execute = execute_mock
                self.run_async(device.process({"ignored": 99}))
                self.assertEqual(self.factory.call_count, 1)
                if asynchronous:
                    execute_mock.assert_awaited_once_with()
                else:
                    execute_mock.assert_called_once_with()
                self.assertIsNone(device.get_capability("unknown"))
                self.assertFalse(device.busy)
                instance.busy = True
                self.assertTrue(device.busy)
                self.run_async(device.stop())
                instance.stop.assert_called_once_with()

    def test_pose_rebuilds_scan_and_updates_only_dimension_fields(self):
        device = self.make_device()
        original = device.get_capability("scan")
        pose = {"start": 0, "stop": 10, "steps": 5, "velocity": 2}
        self.run_async(device.process(dict(pose, unknown=99)))
        self.assertEqual(self.run_async(device.snapshot()), pose)
        self.assertEqual(self.factory.call_count, 2)
        scantype, config, data_dir = self.factory.call_args.args
        self.assertEqual(scantype, "linear")
        self.assertIsNone(data_dir)
        self.assertEqual(config.actuators["energy"].pv, "TEST:ENERGY")
        self.assertEqual(config.scan_dimensions[0].actuator, "energy")
        self.assertFalse(hasattr(config.scan_dimensions[0], "unknown"))
        original.execute.assert_not_called()
        device.get_capability("scan").execute.assert_called_once_with()

    def test_empty_dimensions_and_missing_scan(self):
        device = self.make_device()
        device._config.scan_dimensions = []
        self.assertEqual(self.run_async(device.snapshot()), {})
        self.run_async(device.process({"start": 1}))
        self.assertEqual(self.factory.call_count, 1)
        device._scan = None
        self.assertIsNone(device.get_capability("scan"))
        with self.assertRaisesRegex(RuntimeError, "could not be created"):
            self.run_async(device.process({}))

    def test_execute_failure_propagates(self):
        device = self.make_device()
        device.get_capability("scan").execute.side_effect = RuntimeError("scan failed")
        with self.assertRaisesRegex(RuntimeError, "scan failed"):
            self.run_async(device.process({}))

    def test_replacement_closes_previous_scan_and_final_close_is_idempotent(self):
        device = self.make_device()
        previous = device.get_capability("scan")
        self.run_async(device.process({"start": 0}))
        current = device.get_capability("scan")
        previous.close.assert_called_once_with()
        current.close.assert_not_called()
        self.run_async(device.close())
        self.run_async(device.close())
        previous.close.assert_called_once_with()
        current.close.assert_called_once_with()

    def test_replacement_construction_failure_preserves_scan_and_configuration(self):
        device = self.make_device()
        previous = device.get_capability("scan")
        parameters = self.run_async(device.snapshot())
        error = RuntimeError("replacement construction failed")
        self.factory.side_effect = error
        with self.assertRaises(RuntimeError) as caught:
            self.run_async(device.process({"start": 1}))
        self.assertIs(caught.exception, error)
        self.assertIs(device.get_capability("scan"), previous)
        self.assertEqual(self.run_async(device.snapshot()), parameters)
        previous.close.assert_not_called()
        self.run_async(device.close())
        previous.close.assert_called_once_with()

    def test_replacement_factory_returning_none_preserves_current_scan(self):
        device = self.make_device()
        previous = device.get_capability("scan")
        parameters = self.run_async(device.snapshot())
        self.factory.side_effect = None
        self.factory.return_value = None
        with self.assertRaisesRegex(RuntimeError, "could not be created"):
            self.run_async(device.process({"stop": 20}))
        self.assertIs(device.get_capability("scan"), previous)
        self.assertEqual(self.run_async(device.snapshot()), parameters)
        self.run_async(device.close())
        previous.close.assert_called_once_with()

    def test_failed_previous_disposal_retains_both_scans_for_final_cleanup(self):
        device = self.make_device()
        previous = device.get_capability("scan")
        previous.close.side_effect = [RuntimeError("close failed"), None]
        with self.assertRaisesRegex(RuntimeError, "close failed"):
            self.run_async(device.process({"steps": 8}))
        current = device.get_capability("scan")
        self.assertIsNot(current, previous)
        current.execute.assert_not_called()
        self.run_async(device.close())
        self.assertEqual(previous.close.call_count, 2)
        current.close.assert_called_once_with()

    def test_cancelled_scan_waiter_does_not_allow_close_before_worker_finishes(self):
        device = self.make_device()
        instance = device.get_capability("scan")
        started = threading.Event()
        finish = threading.Event()

        def execute():
            started.set()
            finish.wait(2)

        instance.execute.side_effect = execute

        async def exercise():
            operation = asyncio.create_task(device.process({}))
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(started.is_set())
            operation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await operation
            closing = asyncio.create_task(device.close())
            await asyncio.sleep(0.01)
            instance.close.assert_not_called()
            finish.set()
            await closing
            instance.close.assert_called_once_with()

        try:
            self.run_async(exercise())
        finally:
            finish.set()

    def test_cancelled_replacement_construction_keeps_returned_scan_owned(self):
        device = self.make_device()
        previous = device.get_capability("scan")
        replacement = SimpleNamespace(execute=Mock(), stop=Mock(), close=Mock(), busy=False)
        started = threading.Event()
        finish = threading.Event()

        def construct(*_args):
            started.set()
            finish.wait(2)
            return replacement

        self.factory.side_effect = construct

        async def exercise():
            operation = asyncio.create_task(device.process({"start": 0}))
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(started.is_set())
            operation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await operation
            self.assertIs(device.get_capability("scan"), previous)
            closing = asyncio.create_task(device.close())
            await asyncio.sleep(0.01)
            replacement.close.assert_not_called()
            finish.set()
            await closing
            previous.close.assert_called_once_with()
            replacement.close.assert_called_once_with()
            replacement.execute.assert_not_called()

        try:
            self.run_async(exercise())
        finally:
            finish.set()

    def test_cancelled_async_scan_remains_tracked_until_execution_settles(self):
        device = self.make_device()
        instance = device.get_capability("scan")

        async def exercise():
            started = asyncio.Event()
            finish = asyncio.Event()

            async def execute():
                started.set()
                await finish.wait()

            instance.execute = execute
            operation = asyncio.create_task(device.process({}))
            await started.wait()
            operation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await operation
            closing = asyncio.create_task(device.close())
            await asyncio.sleep(0)
            instance.close.assert_not_called()
            finish.set()
            await closing
            instance.close.assert_called_once_with()

        self.run_async(exercise())

    def test_closed_scan_rejects_execution_and_replacement(self):
        device = self.make_device()
        instance = device.get_capability("scan")
        self.run_async(device.close())
        with self.assertRaisesRegex(RuntimeError, "clos"):
            self.run_async(device.process({"start": 0}))
        self.assertEqual(self.factory.call_count, 1)
        instance.execute.assert_not_called()
        instance.close.assert_called_once_with()

    def test_scheduler_scan_replacement_settles_without_waiting_for_itself(self):
        device = self.make_device()
        previous = device.get_capability("scan")

        async def exercise():
            scheduler = Scheduler([MoveStep(device, {"start": 0}, group_id=0, rollback=True)])
            await asyncio.wait_for(scheduler.run(), timeout=1)
            await asyncio.wait_for(device.close(), timeout=1)

        self.run_async(exercise())
        previous.close.assert_called_once_with()
        current = device.get_capability("scan")
        current.execute.assert_called_once_with()
        current.close.assert_called_once_with()

    def test_rollback_is_unsupported_but_explicit_snapshots_remain_available(self):
        device = self.make_device(start=1, stop=2, steps=4, velocity=0.5)
        expected = {"start": 1, "stop": 2, "steps": 4, "velocity": 0.5}
        self.assertFalse(device.supports_rollback())
        self.assertEqual(self.run_async(device.snapshot()), expected)
        scheduler = Scheduler([MoveStep(device, {}, 0, rollback=True)])
        self.assertEqual(self.run_async(scheduler.snapshot()), {"scan": expected})
        device.get_capability("scan").execute.assert_not_called()

    def test_later_failure_skips_scan_replay_and_snapshot_but_rolls_back_motion(self):
        device = self.make_device()
        instance = device.get_capability("scan")
        process = self.patch(device, "process", AsyncMock(wraps=device.process))
        snapshot = self.patch(device, "snapshot", AsyncMock(wraps=device.snapshot))
        first = SimDevice("first", {"position": 10}, delay=0)
        second = SimDevice("second", {"position": 20}, delay=0)
        broken = SimDevice("broken", delay=0)
        movements = Mock()
        for motor in (first, second):
            movements.attach_mock(
                self.patch(motor, "process", AsyncMock(wraps=motor.process)),
                motor.name,
            )
        failure = RuntimeError("later step failed")
        self.patch(broken, "process", AsyncMock(side_effect=failure))
        scheduler = Scheduler([
            MoveStep(first, {"position": 1}, 0, rollback=True),
            MoveStep(device, {}, 1, rollback=True),
            MoveStep(second, {"position": 2}, 2, rollback=True),
            MoveStep(broken, {}, 3, rollback=True),
        ], cleanup_timeout=0.1)

        async def run_owned_scan():
            async with DeviceScope() as owned:
                owned.add(device)
                await scheduler.run()

        with self.assertLogs("seq_quince.scheduler", level="ERROR"), self.assertRaises(MoveError) as caught:
            self.run_async(run_owned_scan())

        self.assertIs(caught.exception.__cause__, failure)
        process.assert_awaited_once_with({})
        snapshot.assert_not_awaited()
        instance.execute.assert_called_once_with()
        instance.close.assert_called_once_with()
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(movements.mock_calls, [
            call.first({"position": 1}), call.second({"position": 2}),
            call.second({"position": 20}), call.first({"position": 10}),
        ])
        self.assertEqual(self.run_async(first.snapshot()), {"position": 10})
        self.assertEqual(self.run_async(second.snapshot()), {"position": 20})

    def test_active_scan_is_stopped_on_parallel_failure_and_cancellation(self):
        async def scenario(cancel):
            device = self.make_device()
            instance = device.get_capability("scan")
            snapshot = self.patch(device, "snapshot", AsyncMock(wraps=device.snapshot))
            executed = Mock()
            failure = RuntimeError("parallel step failed")
            started = asyncio.Event()
            stopped = asyncio.Event()
            settled = asyncio.Event()

            async def execute():
                executed()
                instance.busy = True
                started.set()
                try:
                    await stopped.wait()
                finally:
                    instance.busy = False
                    settled.set()

            # Use an async def so Python 3.8 detects async scan engines.
            instance.execute = execute
            loop = asyncio.get_running_loop()
            instance.stop.side_effect = lambda: loop.call_soon_threadsafe(stopped.set)

            def close():
                self.assertTrue(settled.is_set())
                self.assertFalse(instance.busy)

            instance.close.side_effect = close
            steps = [MoveStep(device, {}, 0, rollback=True)]
            if not cancel:
                broken = SimDevice("broken", delay=0)

                async def fail(pose):
                    await started.wait()
                    raise failure

                self.patch(broken, "process", AsyncMock(side_effect=fail))
                steps.append(MoveStep(broken, {}, 0, rollback=True))
            scheduler = Scheduler(steps, cleanup_timeout=0.1)
            with self.assertLogs("seq_quince.scheduler", level="WARNING"):
                async def run_owned_scan():
                    async with DeviceScope() as owned:
                        owned.add(device)
                        await scheduler.run()

                running = asyncio.create_task(run_owned_scan())
                await asyncio.wait_for(started.wait(), 1)
                if cancel:
                    running.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(running, 1)
                else:
                    with self.assertRaises(MoveError) as caught:
                        await asyncio.wait_for(running, 1)
                    self.assertIs(caught.exception.__cause__, failure)
            instance.stop.assert_called_once_with()
            instance.close.assert_called_once_with()
            executed.assert_called_once_with()
            snapshot.assert_not_awaited()
            self.assertTrue(settled.is_set())
            self.assertFalse(device.busy)

        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                self.run_async(scenario(cancel))
