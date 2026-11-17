"""The lifecycle context: process-wide bookkeeping and the state lookup hook."""

import asyncio
import threading
from unittest.mock import Mock

from seq_quince.device import lifecycle
from seq_quince.device.device_abs import DeviceABC
from seq_quince.device.lifecycle import (
    CleanupError,
    DeviceScope,
    LifecycleContext,
    has_pending_work,
    lifecycle_context,
    mark_unconfirmed,
    retained_devices,
    run_blocking,
    set_lifecycle_context,
    track_resource,
)

from ._support import CoreTestCase


class AxisDevice(DeviceABC):
    """Two objects with the same axis name drive the same hardware."""

    _cleanup_tracks_work = True

    def __init__(self, name, axis):
        self.name = name
        self.axis = axis
        self.driver = Mock()
        track_resource(self, self.driver, self.driver.close)

    @classmethod
    def from_config_dict(cls, config):
        return cls(config["name"], config["axis"])

    async def process(self, pose):
        pass

    async def snapshot(self):
        return {}

    @property
    def busy(self):
        return has_pending_work(self)

    async def stop(self):
        pass


class SharedAxisContext(LifecycleContext):
    """Like a future session: one lifecycle state per physical axis."""

    def __init__(self):
        super().__init__()
        self.axes = {}

    def state_for(self, owner):
        if owner.axis not in self.axes:
            self.axes[owner.axis] = lifecycle._State()
        return self.axes[owner.axis]


class LifecycleContextTests(CoreTestCase):
    def install(self, context):
        previous = set_lifecycle_context(context)
        self.addCleanup(set_lifecycle_context, previous)

    def test_default_context_keeps_one_state_per_device_object(self):
        first = AxisDevice("first", "theta")
        second = AxisDevice("second", "theta")
        self.assertIs(lifecycle_context().state_for(first), first._resource_lifecycle)
        self.assertIsNot(first._resource_lifecycle, second._resource_lifecycle)
        self.run_async(asyncio.gather(first.close(), second.close()))

    def test_state_lookup_can_share_one_tracker_between_users_of_an_axis(self):
        self.install(SharedAxisContext())
        release = threading.Event()

        async def scenario():
            loop = asyncio.get_running_loop()
            started = asyncio.Event()
            outer = AxisDevice("outer scan", "theta")
            nested = AxisDevice("nested scan", "theta")
            other = AxisDevice("other axis", "chi")

            def move():
                loop.call_soon_threadsafe(started.set)
                if not release.wait(2):
                    raise TimeoutError("test worker not released")

            moving = asyncio.create_task(run_blocking(outer, move))
            try:
                await asyncio.wait_for(started.wait(), 1)
                # The nested user sees the outer user's command on the same axis.
                self.assertTrue(nested.busy)
                self.assertFalse(other.busy)
                release.set()
                await asyncio.wait_for(moving, 1)
                self.assertFalse(nested.busy)
            finally:
                release.set()
            # Shared state also means shared ownership: closing one user closes
            # the axis for both. Borrowing without closing needs leases.
            await outer.close()
            self.assertTrue(lifecycle_context().state_for(nested).closed)
            nested.driver.close.assert_called_once_with()
            await other.close()

        self.run_async(scenario())

    def test_each_context_keeps_its_own_retained_devices_and_scope(self):
        first = LifecycleContext()
        self.install(first)
        held = AxisDevice("held", "theta")
        mark_unconfirmed(held, "stop request failed", hold=True)
        with self.assertLogs(level="ERROR"), self.assertRaises(CleanupError):
            self.run_async(held.close())
        self.assertEqual(retained_devices(), (held,))

        second = LifecycleContext()
        set_lifecycle_context(second)
        self.assertEqual(retained_devices(), ())

        async def scenario():
            async with DeviceScope() as scope:
                built = AxisDevice("built", "chi")
                self.assertIs(second.scope.get(), scope)
                self.assertIsNone(first.scope.get())
            return built

        built = self.run_async(scenario())
        built.driver.close.assert_called_once_with()
        set_lifecycle_context(first)
        self.assertEqual(retained_devices(), (held,))
