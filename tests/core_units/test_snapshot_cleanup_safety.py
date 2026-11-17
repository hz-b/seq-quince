"""Failed external snapshots cannot prove that internal executor work settled."""

import asyncio
import threading
from unittest.mock import AsyncMock

from seq_quince.device import lifecycle
from seq_quince.device.device_abs import DeviceABC
from seq_quince.device.lifecycle import DeviceScope, own_device, retained_devices
from seq_quince.scheduler import MoveError, Scheduler
from seq_quince.sequence import MoveStep

from ._support import CoreTestCase


class ExternalSnapshotDevice(DeviceABC):
    """Legacy extension whose blocking reads do not use lifecycle helpers."""

    name = "external-snapshot"

    def __init__(self, failure):
        self.failure = failure
        self.started = threading.Event()
        self.finish = threading.Event()
        self.worker = None
        self.process_called = False
        self.close = AsyncMock()

    @classmethod
    def from_config_dict(cls, config):
        return cls(config["failure"])

    async def process(self, pose):
        self.process_called = True

    async def snapshot(self):
        def read():
            self.started.set()
            self.finish.wait(2)

        async def fail():
            while not self.started.is_set():
                await asyncio.sleep(0)
            raise self.failure

        self.worker = asyncio.get_running_loop().run_in_executor(None, read)
        await asyncio.gather(self.worker, fail())
        return {}

    @property
    def busy(self):
        # False does not prove that a legacy adapter's hidden worker stopped.
        return False

    async def stop(self):
        pass


class SnapshotCleanupSafetyTests(CoreTestCase):
    def setUp(self):
        super().setUp()
        # These tests deliberately exercise permanent conservative retention.
        self.patch(lifecycle, "_context", lifecycle.LifecycleContext())

    def check_failure(self, rollback_capture, cancellation=False):
        failure = asyncio.CancelledError() if cancellation else ValueError("snapshot failed")
        device = ExternalSnapshotDevice(failure)
        scheduler = Scheduler([MoveStep(device, {}, group_id=0)], cleanup_timeout=0.01)

        async def exercise():
            expected = asyncio.CancelledError if cancellation else (
                MoveError if rollback_capture else ValueError
            )
            with self.assertRaises(expected) as caught:
                async with DeviceScope(timeout=0.05):
                    own_device(device)
                    if rollback_capture:
                        await scheduler.run()
                    else:
                        await scheduler.snapshot()
            if rollback_capture and not cancellation:
                self.assertIs(caught.exception.__cause__, failure)
            elif not cancellation:
                self.assertIs(caught.exception, failure)
            self.assertTrue(device.started.is_set())
            self.assertFalse(device.finish.is_set())
            self.assertFalse(device.process_called)
            device.close.assert_not_awaited()
            self.assertIn(device, retained_devices())
            # The lifecycle cannot inspect the extension's raw executor work.
            # Even its eventual completion cannot discharge that uncertainty.
            device.finish.set()
            await device.worker
            await asyncio.sleep(0)
            device.close.assert_not_awaited()
            self.assertIn(device, retained_devices())

        try:
            self.run_async(exercise())
        finally:
            device.finish.set()

    def test_explicit_snapshot_failure_retains_external_device(self):
        self.check_failure(rollback_capture=False)

    def test_rollback_snapshot_failure_retains_external_device(self):
        self.check_failure(rollback_capture=True)

    def test_self_cancelled_snapshot_retains_external_device(self):
        self.check_failure(rollback_capture=False, cancellation=True)
