import asyncio
import threading
from unittest.mock import patch

from seq_quince.device_concrete import actuator
from seq_quince.scheduler import Scheduler
from seq_quince.sequence import MoveStep

from ._support import DeviceTestCase


class ActuatorDeviceTests(DeviceTestCase):
    def setUp(self):
        super().setUp()
        self.init_actuators()

    def test_move_configuration_and_both_executor_paths(self):
        for legacy in (False, True):
            with self.subTest(legacy_executor=legacy):
                device = actuator.ActuatorDevice.from_config_dict(
                    {
                        "name": "theta",
                        "config": {"pv": "TEST:THETA", "queueing_delay": 0.02},
                    }
                )
                driver = self.drivers[-1]
                # patch.dict restores the actual absence/presence on Python 3.8+.
                with patch.dict(asyncio.__dict__):
                    if legacy:
                        asyncio.__dict__.pop("to_thread", None)
                    self.run_async(device.process({"position": 0.0}))
                driver.run_move.assert_called_once_with(0.0, True)
                config = self.driver_factory.call_args.args[0]
                self.assertEqual(config.pv, "TEST:THETA")
                self.assertEqual(config.queueing_delay, 0.02)
                self.assertIs(device.get_capability("actuator"), driver)
                self.assertIsNone(device.get_capability("unknown"))

    def test_readback_and_ready_state(self):
        device = actuator.ActuatorDevice("theta", {"pv": "TEST:THETA"})
        driver = self.drivers[0]
        driver.rbv = 12.5
        self.assertEqual(self.run_async(device.snapshot()), {"position": 12.5})
        self.assertFalse(device.busy)
        driver.is_ready.return_value = False
        self.run_async(device.snapshot())
        self.assertTrue(device.busy)
        calls = driver.is_ready.call_count
        self.assertTrue(device.busy)
        self.assertEqual(driver.is_ready.call_count, calls)
        driver.is_ready.return_value = True
        self.run_async(device.snapshot())

    def test_invalid_pose_never_moves_and_driver_error_propagates(self):
        device = actuator.ActuatorDevice("theta", {"pv": "TEST:THETA"})
        driver = self.drivers[0]
        with self.assertRaisesRegex(ValueError, "position"):
            self.run_async(device.process({}))
        driver.run_move.assert_not_called()
        error = RuntimeError("motor fault")
        driver.run_move.side_effect = error
        with self.assertRaises(RuntimeError) as caught:
            self.run_async(device.process({"position": 2}))
        self.assertIs(caught.exception, error)

    def test_stop_reaches_driver_without_claiming_motion_has_ceased(self):
        device = actuator.ActuatorDevice("theta", {"pv": "TEST:THETA"})
        driver = self.drivers[0]
        driver.is_ready.return_value = False

        self.run_async(device.stop())

        driver.stop.assert_called_once_with()
        self.assertTrue(device.busy)

    def test_stop_waits_for_driver_without_blocking_event_loop(self):
        device = actuator.ActuatorDevice("theta", {"pv": "TEST:THETA"})
        driver = self.drivers[0]
        release = threading.Event()

        async def scenario():
            loop = asyncio.get_running_loop()
            started = asyncio.Event()
            event_loop_thread = threading.get_ident()
            worker_threads = []

            def stop():
                worker_threads.append(threading.get_ident())
                loop.call_soon_threadsafe(started.set)
                if not release.wait(2):
                    raise TimeoutError("test did not release stop driver")

            driver.stop.side_effect = stop
            stopping = asyncio.create_task(device.stop())
            try:
                await asyncio.wait_for(started.wait(), 1)
                await asyncio.sleep(0)
                self.assertFalse(stopping.done())
                self.assertEqual(len(worker_threads), 1)
                self.assertNotEqual(worker_threads[0], event_loop_thread)
            finally:
                release.set()
                await asyncio.wait_for(stopping, 1)

        self.run_async(scenario())
        driver.stop.assert_called_once_with()

    def test_stop_propagates_driver_error(self):
        device = actuator.ActuatorDevice("theta", {"pv": "TEST:THETA"})
        driver = self.drivers[0]
        error = OSError("stop command failed")
        driver.stop.side_effect = error

        with self.assertRaises(OSError) as caught:
            self.run_async(device.stop())

        self.assertIs(caught.exception, error)
        driver.stop.assert_called_once_with()

    def test_scheduler_cleanup_requires_motion_settlement_after_stop(self):
        for settles in (True, False):
            with self.subTest(motion_settles=settles):
                device = actuator.ActuatorDevice("theta", {"pv": "TEST:THETA"})
                driver = self.drivers[-1]
                release = threading.Event()

                async def scenario(device, driver, release, settles):
                    loop = asyncio.get_running_loop()
                    started = asyncio.Event()
                    worker_done = asyncio.Event()

                    def move(position, wait):
                        loop.call_soon_threadsafe(started.set)
                        try:
                            if not release.wait(2):
                                raise TimeoutError("test did not release move driver")
                        finally:
                            loop.call_soon_threadsafe(worker_done.set)

                    def stop():
                        # A ready readback alone cannot prove worker settlement.
                        driver.is_ready.return_value = True
                        if settles:
                            release.set()

                    driver.run_move.side_effect = move
                    driver.stop.side_effect = stop
                    driver.is_ready.return_value = False
                    scheduler = Scheduler(
                        [MoveStep(device, {"position": 1}, 0)],
                        cleanup_timeout=0.05,
                    )
                    running = asyncio.create_task(scheduler.run())
                    try:
                        await asyncio.wait_for(started.wait(), 1)
                        running.cancel()
                        with self.assertLogs("seq_quince.scheduler", level="WARNING"), self.assertRaises(
                            asyncio.CancelledError
                        ):
                            await asyncio.wait_for(running, 1)
                        driver.stop.assert_called_once_with()
                        expected = () if settles else ("theta",)
                        self.assertEqual(scheduler.unconfirmed_devices, expected)
                        driver.run_move.assert_called_once_with(1, True)
                    finally:
                        release.set()
                        if not running.done():
                            running.cancel()
                        await asyncio.gather(running, return_exceptions=True)
                        await asyncio.wait_for(worker_done.wait(), 1)

                self.run_async(scenario(device, driver, release, settles))
