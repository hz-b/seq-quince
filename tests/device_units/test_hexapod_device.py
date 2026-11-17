"""Verify synchronous EpicsActuator calls through the async device interface."""

import asyncio
from functools import partial
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from seq_quince.device_concrete import hexapod
from seq_quince.scheduler import MoveError, Scheduler
from seq_quince.sequence import MoveStep

from ._support import DeviceTestCase

AXES = ("Tx", "Ty", "Tz", "Rx", "Ry", "Rz")


class HexapodDeviceTests(DeviceTestCase):
    def setUp(self):
        super().setUp()
        self.init_actuators()
        self.config = {"axes": {axis: {"pv": "TEST:" + axis} for axis in AXES}}

    def test_builds_axes_and_reports_any_unready_axis(self):
        device = hexapod.HexapodDevice.from_config_dict(
            {"name": "hex", "config": self.config}
        )
        self.assertEqual(
            [item.args[0].pv for item in self.driver_factory.call_args_list],
            ["TEST:" + axis for axis in AXES],
        )
        self.assertEqual(set(device.get_capability("actuators")), set(AXES))
        self.assertIsNone(device.get_capability("unknown"))
        self.assertTrue(device.busy)  # Readiness is unknown before the first sample.
        self.run_async(device.snapshot())
        self.assertFalse(device.busy)
        self.drivers[-1].is_ready.return_value = False

        async def refresh():
            await device.snapshot()
            self.assertTrue(device.busy)
            calls = sum(driver.is_ready.call_count for driver in self.drivers)
            self.assertTrue(device.busy)
            self.assertEqual(sum(driver.is_ready.call_count for driver in self.drivers), calls)
            self.drivers[-1].is_ready.return_value = True
            while device.busy:
                await asyncio.sleep(0.001)

        self.run_async(refresh())

    def test_missing_axes_are_rejected_before_driver_creation(self):
        with self.assertRaisesRegex(ValueError, "Missing actuator"):
            hexapod.HexapodDevice("hex", {"axes": {"Tx": {"pv": "TEST:Tx"}}})
        self.driver_factory.assert_not_called()

    def test_invalid_run_after_configuration(self):
        cases = [(None, 1, "PV not created"), (SimpleNamespace(disconnect=Mock()), None, "no value defined")]
        for pv, value, message in cases:
            with self.subTest(message=message):
                config = dict(
                    self.config, run_after_pv="TEST:RUN", run_after_value=value
                )
                with patch.object(
                    hexapod, "PV", Mock(return_value=pv)
                ), self.assertRaisesRegex(ValueError, message):
                    hexapod.HexapodDevice("hex", config)
                self.driver_factory.assert_not_called()

    def test_invalid_poses_never_move_any_axis(self):
        cases = [
            (False, {}, "exactly one"),
            (False, {"Tx": 1, "Ty": 2}, "exactly one"),
            (False, {"invalid": 1}, "Unexpected keys"),
            (True, {"Tx": 1}, "missing coordinates"),
            (True, dict.fromkeys(AXES + ("extra",), 1), "unexpected keys"),
            (False, dict.fromkeys(AXES, float("nan")), "finite number"),
            (True, dict.fromkeys(AXES, float("inf")), "finite number"),
            (False, {"Tx": "invalid"}, "finite number"),
        ]
        for coordinated, pose, message in cases:
            with self.subTest(coordinated=coordinated, pose=pose):
                device = hexapod.HexapodDevice(
                    "hex", dict(self.config, enable_coordinated=coordinated)
                )
                with self.assertRaisesRegex(ValueError, message):
                    self.run_async(device.process(pose))
                for driver in self.drivers:
                    driver.run_move.assert_not_called()

    def _check_valid_move(self, coordinated, run_after_value=1):
        run_after = SimpleNamespace(put=Mock(return_value=True), disconnect=Mock())
        self.patch(hexapod, "PV", Mock(return_value=run_after))
        config = dict(
            self.config,
            enable_coordinated=coordinated,
            run_after_pv="TEST:RUN",
            run_after_value=run_after_value,
        )
        device = hexapod.HexapodDevice("hex", config)
        pose = dict.fromkeys(AXES, 2.0) if coordinated else {"Tx": 2.0}
        event_loop_thread = get_ident()

        def move(target, wait):
            run_after.put.assert_called_once_with(run_after_value)
            self.assertEqual((target, wait), (2.0, True))
            self.assertNotEqual(get_ident(), event_loop_thread)

        for driver in self.drivers:
            driver.run_move.side_effect = move
        self.run_async(device.process(pose))
        for axis, driver in zip(AXES, self.drivers):
            self.assertEqual(driver.run_move.call_count, int(axis in pose))

    def test_valid_single_axis_move(self):
        """Run one blocking move outside the event-loop thread."""
        self._check_valid_move(False)

    def test_valid_coordinated_move(self):
        """Run every axis outside the event-loop thread after the run-after put."""
        self._check_valid_move(True)

    def test_zero_run_after_value_is_written_before_coordinated_moves(self):
        self._check_valid_move(True, run_after_value=0)

    def test_coordinated_move_dispatches_other_axes_and_waits_for_completion(self):
        device = hexapod.HexapodDevice(
            "hex", dict(self.config, enable_coordinated=True)
        )

        async def check_move():
            loop = asyncio.get_running_loop()
            started = asyncio.Queue()
            release = Event()

            def move(axis, target, wait):
                self.assertEqual((target, wait), (2.0, True))
                loop.call_soon_threadsafe(started.put_nowait, axis)
                if axis == "Tx" and not release.wait(timeout=5):
                    raise RuntimeError("Timed out waiting for the test to release Tx")

            for axis, driver in zip(AXES, self.drivers):
                driver.run_move.side_effect = partial(move, axis)

            task = asyncio.create_task(device.process(dict.fromkeys(AXES, 2.0)))
            try:
                # Every axis must start while Tx is still blocked; the event
                # loop must also remain able to receive these notifications.
                observed = set()
                for _ in AXES:
                    observed.add(await asyncio.wait_for(started.get(), timeout=5))
                self.assertEqual(observed, set(AXES))
                self.assertFalse(task.done())
            finally:
                release.set()
                await asyncio.wait_for(task, timeout=5)

        self.run_async(check_move())

    def test_move_propagates_driver_failure(self):
        device = hexapod.HexapodDevice("hex", self.config)
        self.drivers[0].run_move.side_effect = RuntimeError("Axis move failed")

        with self.assertRaisesRegex(RuntimeError, "Axis move failed"):
            self.run_async(device.process({"Tx": 2.0}))
        self.drivers[0].run_move.assert_called_once_with(2.0, True)
        for driver in self.drivers[1:]:
            driver.run_move.assert_not_called()

    def test_snapshot_reads_numeric_rbv_properties(self):
        """Read each numeric rbv property without treating it as a coroutine."""
        device = hexapod.HexapodDevice("hex", self.config)
        for index, driver in enumerate(self.drivers):
            driver.rbv = float(index)
        self.assertEqual(
            self.run_async(device.snapshot()),
            {axis: float(i) for i, axis in enumerate(AXES)},
        )

    def test_stop_dispatches_to_all_synchronous_drivers(self):
        """Stop each actuator using its synchronous driver method."""
        device = hexapod.HexapodDevice("hex", self.config)
        self.run_async(device.stop())
        for driver in self.drivers:
            driver.stop.assert_called_once_with()

    def test_full_snapshot_replay_is_sequential_in_axis_order(self):
        device = hexapod.HexapodDevice("hex", self.config)
        pose = {axis: float(index) for index, axis in enumerate(reversed(AXES))}
        events = []

        async def scenario():
            started = asyncio.Event()
            release = Event()
            loop = asyncio.get_running_loop()

            def move(axis, target, wait):
                events.append((axis, target, wait))
                if axis == "Tx":
                    loop.call_soon_threadsafe(started.set)
                    if not release.wait(timeout=5):
                        raise RuntimeError("Test did not release Tx")

            for axis, driver in zip(AXES, self.drivers):
                driver.run_move.side_effect = partial(move, axis)
            task = asyncio.create_task(device.process(pose))
            try:
                await asyncio.wait_for(started.wait(), 2)
                await asyncio.sleep(0)
                self.assertEqual(events, [("Tx", pose["Tx"], True)])
                self.assertFalse(task.done())
            finally:
                release.set()
                await asyncio.wait_for(task, 2)
            self.assertEqual(events, [(axis, pose[axis], True) for axis in AXES])

        self.run_async(scenario())

    def test_invalid_last_target_prevents_run_after_and_all_moves(self):
        run_after = SimpleNamespace(put=Mock(return_value=True), disconnect=Mock())
        self.patch(hexapod, "PV", Mock(return_value=run_after))
        device = hexapod.HexapodDevice(
            "hex", dict(self.config, run_after_pv="TEST:RUN", run_after_value=1)
        )
        pose = dict.fromkeys(AXES, 0.0)
        pose["Rz"] = float("nan")
        with self.assertRaisesRegex(ValueError, "Rz.*finite"):
            self.run_async(device.process(pose))
        run_after.put.assert_not_called()
        for driver in self.drivers:
            driver.run_move.assert_not_called()

    def test_stop_waits_for_every_axis_and_reports_failures(self):
        device = hexapod.HexapodDevice("hex", self.config)
        self.drivers[0].stop.side_effect = OSError("Tx stop failed")

        async def scenario():
            started = asyncio.Event()
            release = Event()
            loop = asyncio.get_running_loop()

            def stop():
                loop.call_soon_threadsafe(started.set)
                if not release.wait(timeout=5):
                    raise RuntimeError("Test did not release stop")
                raise ValueError("Rz stop failed")

            self.drivers[-1].stop.side_effect = stop
            task = asyncio.create_task(device.stop())
            try:
                await asyncio.wait_for(started.wait(), 2)
                self.assertFalse(task.done())
            finally:
                release.set()
                with self.assertRaisesRegex(RuntimeError, "Tx.*Tx stop failed.*Rz.*Rz stop failed"):
                    await asyncio.wait_for(task, 2)

        self.run_async(scenario())
        for driver in self.drivers:
            driver.stop.assert_called_once_with()

    def test_snapshot_readiness_checks_leave_loop_responsive(self):
        device = hexapod.HexapodDevice("hex", self.config)

        async def scenario():
            started = asyncio.Event()
            release = Event()
            loop = asyncio.get_running_loop()
            loop_thread = get_ident()

            def ready():
                self.assertNotEqual(get_ident(), loop_thread)
                loop.call_soon_threadsafe(started.set)
                if not release.wait(timeout=5):
                    raise RuntimeError("Test did not release readiness read")
                return True

            self.drivers[-1].is_ready.side_effect = ready
            task = asyncio.create_task(device.snapshot())
            try:
                await asyncio.wait_for(started.wait(), 2)
                self.assertFalse(task.done())
                # This must read only cached state while the hardware read waits.
                self.assertTrue(device.busy)
            finally:
                release.set()
                await asyncio.wait_for(task, 2)

        self.run_async(scenario())

    def test_cancelled_stop_waiter_retains_all_six_workers_until_cleanup(self):
        device = hexapod.HexapodDevice("hex", self.config)

        async def scenario():
            entered = asyncio.Queue()
            release = Event()
            loop = asyncio.get_running_loop()

            def stop(axis):
                loop.call_soon_threadsafe(entered.put_nowait, axis)
                if not release.wait(timeout=5):
                    raise RuntimeError("Test did not release axis stop")

            for axis, driver in zip(AXES, self.drivers):
                driver.stop.side_effect = partial(stop, axis)
            task = asyncio.create_task(device.stop())
            closing = None
            try:
                observed = set()
                for _ in AXES:
                    observed.add(await asyncio.wait_for(entered.get(), 2))
                self.assertEqual(observed, set(AXES))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                closing = asyncio.create_task(device.close())
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(closing), 0.02)
                self.assertTrue(device.busy)
                for driver in self.drivers:
                    driver.stop.assert_called_once_with()
                    driver.close.assert_not_called()
            finally:
                release.set()
                if closing is not None:
                    await asyncio.wait_for(closing, 2)
                elif not task.done():
                    await asyncio.wait_for(task, 2)
            for driver in self.drivers:
                driver.close.assert_called_once_with()

        self.run_async(scenario())

    def test_completed_stop_request_keeps_readiness_observer_and_resources(self):
        device = hexapod.HexapodDevice("hex", self.config)
        self.drivers[-1].is_ready.return_value = False

        async def scenario():
            await device.stop()
            for driver in self.drivers:
                driver.stop.assert_called_once_with()
            self.assertTrue(device.busy)
            closing = asyncio.create_task(device.close())
            try:
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(closing), 0.02)
                for driver in self.drivers:
                    driver.close.assert_not_called()
            finally:
                self.drivers[-1].is_ready.return_value = True
                await asyncio.wait_for(closing, 2)
            self.assertFalse(device.busy)

        self.run_async(scenario())

    def test_cancellation_during_run_after_does_not_dispatch_moves(self):
        run_after = SimpleNamespace(put=Mock(), disconnect=Mock())
        self.patch(hexapod, "PV", Mock(return_value=run_after))
        device = hexapod.HexapodDevice(
            "hex", dict(self.config, run_after_pv="TEST:RUN", run_after_value=1)
        )

        async def scenario():
            started = asyncio.Event()
            release = Event()
            loop = asyncio.get_running_loop()

            def put(value):
                self.assertEqual(value, 1)
                loop.call_soon_threadsafe(started.set)
                if not release.wait(timeout=5):
                    raise RuntimeError("Test did not release run-after put")

            run_after.put.side_effect = put
            task = asyncio.create_task(device.process({"Tx": 2.0}))
            closing = None
            try:
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                closing = asyncio.create_task(device.close())
                await asyncio.sleep(0)
                run_after.disconnect.assert_not_called()
            finally:
                release.set()
                if closing is not None:
                    await asyncio.wait_for(closing, 2)
            run_after.disconnect.assert_called_once_with()
            for driver in self.drivers:
                driver.run_move.assert_not_called()

        self.run_async(scenario())

    def test_cancellation_tracks_worker_and_does_not_dispatch_next_axis(self):
        device = hexapod.HexapodDevice("hex", self.config)

        async def scenario():
            started = asyncio.Event()
            release = Event()
            loop = asyncio.get_running_loop()

            def move(target, wait):
                loop.call_soon_threadsafe(started.set)
                if not release.wait(timeout=5):
                    raise RuntimeError("Test did not release Tx")

            self.drivers[0].run_move.side_effect = move
            task = asyncio.create_task(device.process(dict.fromkeys(AXES, 0.0)))
            closing = None
            try:
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                closing = asyncio.create_task(device.close())
                await asyncio.sleep(0)
                for driver in self.drivers:
                    driver.close.assert_not_called()
                self.assertFalse(closing.done())
            finally:
                release.set()
                if closing is not None:
                    await asyncio.wait_for(closing, 2)
            for driver in self.drivers[1:]:
                driver.run_move.assert_not_called()
            for driver in self.drivers:
                driver.close.assert_called_once_with()

        self.run_async(scenario())

    def _failing_device(self):
        return SimpleNamespace(
            name="broken", process=AsyncMock(side_effect=RuntimeError("later failure")),
            snapshot=AsyncMock(return_value={}), stop=AsyncMock(), busy=False,
            scheduler_timeout=Mock(return_value=None), supports_rollback=Mock(return_value=True),
        )

    def test_scheduler_rolls_back_completed_single_axis_move(self):
        device = hexapod.HexapodDevice("hex", self.config)
        events = []
        for axis, driver in zip(AXES, self.drivers):
            driver.rbv = float(AXES.index(axis))

            def move(target, wait, axis=axis, driver=driver):
                events.append((axis, target))
                driver.rbv = target

            driver.run_move.side_effect = move
        scheduler = Scheduler([
            MoveStep(device, {"Ty": 20.0}, 0, rollback=True),
            MoveStep(self._failing_device(), {}, 1, rollback=True),
        ])
        with self.assertLogs("seq_quince.scheduler", level="ERROR"), self.assertRaises(MoveError):
            self.run_async(scheduler.run())
        self.assertEqual(events, [("Ty", 20.0)] + [
            (axis, float(index)) for index, axis in enumerate(AXES)
        ])
        self.assertEqual(self.run_async(device.snapshot()), {
            axis: float(index) for index, axis in enumerate(AXES)
        })
        self.assertEqual(scheduler.unconfirmed_devices, ("broken",))

    def test_restoration_failure_uses_scheduler_stop_recovery(self):
        device = hexapod.HexapodDevice("hex", self.config)
        self.drivers[1].run_move.side_effect = [None, RuntimeError("restore failed")]
        scheduler = Scheduler([
            MoveStep(device, {"Ty": 20.0}, 0, rollback=True),
            MoveStep(self._failing_device(), {}, 1, rollback=True),
        ])
        with self.assertLogs("seq_quince.scheduler", level="ERROR") as logs, self.assertRaises(MoveError):
            self.run_async(scheduler.run())
        self.assertIn("Rollback failed for hex", "\n".join(logs.output))
        for driver in self.drivers:
            driver.stop.assert_called_once_with()
        for driver in self.drivers[2:]:
            driver.run_move.assert_not_called()

    def test_restoration_timeout_stops_before_later_axes(self):
        device = hexapod.HexapodDevice("hex", self.config)
        release = Event()

        def restore(target, wait):
            if not release.wait(timeout=5):
                raise RuntimeError("Test did not stop Tx")

        self.drivers[0].run_move.side_effect = restore
        self.drivers[0].stop.side_effect = release.set
        scheduler = Scheduler([
            MoveStep(device, {"Ty": 20.0}, 0, rollback=True),
            MoveStep(self._failing_device(), {}, 1, rollback=True),
        ], cleanup_timeout=0.02)
        try:
            with self.assertLogs("seq_quince.scheduler", level="ERROR") as logs, self.assertRaises(MoveError):
                self.run_async(scheduler.run())
        finally:
            release.set()
        self.assertIn("Rollback timed out for hex", "\n".join(logs.output))
        for driver in self.drivers:
            driver.stop.assert_called_once_with()
        self.drivers[1].run_move.assert_called_once_with(20.0, True)
        for driver in self.drivers[2:]:
            driver.run_move.assert_not_called()

    def test_caller_cancellation_preserves_rollback_recovery(self):
        device = hexapod.HexapodDevice("hex", self.config)
        scheduler = Scheduler([
            MoveStep(device, {"Ty": 20.0}, 0, rollback=True),
            MoveStep(self._failing_device(), {}, 1, rollback=True),
        ])

        async def scenario():
            restoring = asyncio.Event()
            release = Event()
            loop = asyncio.get_running_loop()

            def restore(target, wait):
                loop.call_soon_threadsafe(restoring.set)
                if not release.wait(timeout=5):
                    raise RuntimeError("Test did not release restoration")

            self.drivers[0].run_move.side_effect = restore
            with self.assertLogs("seq_quince.scheduler", level="ERROR"):
                task = asyncio.create_task(scheduler.run())
                try:
                    await asyncio.wait_for(restoring.wait(), 2)
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                finally:
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 2)
            self.assertEqual(self.drivers[1].run_move.call_count, 2)
            for driver in self.drivers[2:]:
                driver.run_move.assert_called_once_with(0.0, True)

        self.run_async(scenario())
