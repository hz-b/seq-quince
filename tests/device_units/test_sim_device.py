import asyncio

from seq_quince.device_concrete import sim

from ._support import DeviceTestCase


class SimDeviceTests(DeviceTestCase):
    def test_configuration_and_snapshot_copies(self):
        initial = {"x": 1.0, "y": 2.0}
        device = sim.SimDevice.from_config_dict(
            {"name": "sim", "initial_pose": initial, "delay": "0"}
        )
        initial["x"] = 99
        snapshot = self.run_async(device.snapshot())
        self.assertEqual(snapshot, {"x": 1.0, "y": 2.0})
        snapshot["y"] = 99
        self.assertEqual(self.run_async(device.snapshot()), {"x": 1.0, "y": 2.0})
        default = sim.SimDevice.from_config_dict({"name": "default"})
        self.assertEqual(self.run_async(default.snapshot()), {"position": 0.0})

    def test_move_updates_only_supplied_axes_and_exposes_busy(self):
        device = sim.SimDevice("sim", {"x": 1, "y": 2}, delay=0.25)

        async def sleep(delay):
            self.assertEqual(delay, 0.25)
            self.assertTrue(device.busy)
            self.assertEqual(await device.snapshot(), {"x": 1, "y": 2})

        self.patch(sim.asyncio, "sleep", sleep)
        self.run_async(device.process({"x": 3, "z": 4}))
        self.assertEqual(self.run_async(device.snapshot()), {"x": 3, "y": 2, "z": 4})
        self.assertFalse(device.busy)

    def test_stop_clears_busy_during_pending_delay(self):
        device = sim.SimDevice("sim", delay=0)

        async def sleep(_delay):
            self.assertTrue(device.busy)
            await device.stop()
            self.assertFalse(device.busy)

        self.patch(sim.asyncio, "sleep", sleep)
        self.run_async(device.process({"position": 5}))
        self.assertEqual(self.run_async(device.snapshot()), {"position": 5})

    def test_failed_processing_clears_busy_and_preserves_previous_pose(self):
        device = sim.SimDevice("sim", {"position": 1})

        async def fail(_delay):
            self.assertTrue(device.busy)
            raise RuntimeError("simulated failure")

        self.patch(sim.asyncio, "sleep", fail)
        with self.assertRaisesRegex(RuntimeError, "simulated failure"):
            self.run_async(device.process({"position": 5}))
        self.assertFalse(device.busy)
        self.assertEqual(self.run_async(device.snapshot()), {"position": 1})

    def test_cancellation_clears_busy_without_applying_pending_pose(self):
        device = sim.SimDevice("sim", {"position": 1})

        async def check_cancellation():
            started = asyncio.Event()
            release = asyncio.Event()

            async def wait(_delay):
                started.set()
                await release.wait()

            self.patch(sim.asyncio, "sleep", wait)
            task = asyncio.create_task(device.process({"position": 5}))
            await asyncio.wait_for(started.wait(), timeout=1)
            self.assertTrue(device.busy)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(device.busy)
            self.assertEqual(await device.snapshot(), {"position": 1})

        self.run_async(check_cancellation())
