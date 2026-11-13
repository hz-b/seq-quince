"""Cached readiness must follow native workers beyond waiter cancellation."""

import asyncio
import threading
from unittest.mock import Mock

from seq_quince.device.device_abs import DeviceABC
from seq_quince.device.lifecycle import (
    MotionState,
    has_pending_work,
    mark_unconfirmed,
    run_blocking,
    track_resource,
)

from ._support import CoreTestCase


class TrackedMotionDevice(DeviceABC):
    name = "tracked motion"
    _cleanup_tracks_work = True

    @classmethod
    def from_config_dict(cls, config):
        return cls()

    async def process(self, pose):
        pass

    async def snapshot(self):
        return {}

    @property
    def busy(self):
        return self.motion.busy or has_pending_work(self)

    async def stop(self):
        pass


class MotionTrackingTests(CoreTestCase):
    def test_observer_waits_for_worker_before_accepting_idle_hardware(self):
        release = threading.Event()
        moving = threading.Event()
        device = TrackedMotionDevice()
        device.motion = MotionState(device, moving.is_set)
        resource = Mock()
        track_resource(device, resource, resource.close)

        async def scenario():
            started = asyncio.Event()
            sampled = asyncio.Event()
            loop = asyncio.get_running_loop()

            def work():
                loop.call_soon_threadsafe(started.set)
                if not release.wait(2):
                    raise TimeoutError("test worker not released")
                moving.set()

            def check():
                loop.call_soon_threadsafe(sampled.set)
                return moving.is_set()

            device.motion.checker = check
            running = asyncio.create_task(run_blocking(device, work))
            closing = None
            try:
                await asyncio.wait_for(started.wait(), 1)
                running.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await running
                device.motion.watch()
                await asyncio.wait_for(sampled.wait(), 1)
                self.assertTrue(device.busy)
                closing = asyncio.create_task(device.close())
                await asyncio.sleep(0)
                resource.close.assert_not_called()
                release.set()
                # The worker dispatches after the observer first saw idle.
                while has_pending_work(device):
                    await asyncio.sleep(0.001)
                self.assertTrue(device.busy)
                self.assertFalse(closing.done())
                moving.clear()
                await asyncio.wait_for(closing, 1)
                resource.close.assert_called_once_with()
            finally:
                release.set()
                moving.clear()
                if closing is not None:
                    await asyncio.wait_for(closing, 1)

        self.run_async(scenario())

    def test_unconfirmed_cleanup_does_not_count_its_own_busy_probe(self):
        device = TrackedMotionDevice()
        device.motion = MotionState(device, lambda: False)
        resource = Mock()
        track_resource(device, resource, resource.close)

        async def scenario():
            await device.motion.refresh()
            mark_unconfirmed(device, "operation failed; internal work may remain")
            await asyncio.wait_for(device.close(), 1)

        self.run_async(scenario())
        resource.close.assert_called_once_with()
