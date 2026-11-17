"""Runner resource ownership and safe disposal without Channel Access."""

import asyncio
import threading
from unittest.mock import AsyncMock, Mock, patch

from seq_quince import runner
from seq_quince.config_manager import SchedulerConfigManager
from seq_quince.device import lifecycle as lifecycle_module
from seq_quince.device.device_abs import DeviceABC
from seq_quince.device.factory import DEVICE_REGISTRY, DeviceFactory
from seq_quince.device.lifecycle import (
    CleanupError,
    DeviceScope,
    ensure_open,
    mark_unconfirmed,
    own_device,
    retained_devices,
    run_blocking,
    track_resource,
)
from seq_quince.scheduler import MoveError, Scheduler
from seq_quince.sequence import MoveStep, SequenceError

from ._support import CoreTestCase


class ResourceDevice(DeviceABC):
    """An adapter with a mocked owned driver and fully tracked blocking work."""

    _cleanup_tracks_work = True
    created = None

    def __init__(self, config):
        self.name = config["name"]
        self.config = config
        self.driver = Mock()
        self.driver.close = Mock(side_effect=config.get("close"))
        track_resource(self, self.driver, self.driver.close)
        self.created.append(self)
        if config.get("construct_error"):
            raise config["construct_error"]

    @classmethod
    def from_config_dict(cls, config):
        return cls(config)

    async def process(self, pose):
        operation = self.config.get("process")
        if operation:
            await operation(self, pose)

    async def snapshot(self):
        operation = self.config.get("snapshot")
        if operation:
            return await operation(self)
        return {"position": 1.0}

    @property
    def busy(self):
        return False

    async def stop(self):
        operation = self.config.get("stop")
        if operation:
            await operation(self)

    def supports_rollback(self):
        return False


class LegacyDevice(DeviceABC):
    """An existing extension that never needed to implement close()."""

    name = "legacy"

    @classmethod
    def from_config_dict(cls, config):
        return cls()

    async def process(self, pose):
        pass

    async def snapshot(self):
        return {}

    @property
    def busy(self):
        return False

    async def stop(self):
        pass


class ResourceCleanupTests(CoreTestCase):
    def setUp(self):
        super().setUp()
        self.created = []
        self.patch(ResourceDevice, "created", self.created)
        self.patch(DeviceFactory, "load_extensions", Mock())
        registry = patch.dict(DEVICE_REGISTRY, {"CleanupTestDevice": ResourceDevice})
        registry.start()
        self.addCleanup(registry.stop)

    def manager(self, configs=None, preset=None):
        manager = SchedulerConfigManager.__new__(SchedulerConfigManager)
        manager.devices_cfg = configs or [{"name": "first"}, {"name": "second"}]
        for config in manager.devices_cfg:
            config.setdefault("type", "CleanupTestDevice")
        manager.replacements = {}
        manager.presets = {
            "run": preset if preset is not None else {
                config["name"]: {} for config in manager.devices_cfg
            }
        }
        manager.save_snapshot = Mock()
        return manager

    def assert_closed_once(self):
        self.assertTrue(self.created)
        for device in self.created:
            device.driver.close.assert_called_once_with()

    def test_run_success_closes_selected_and_unused_devices(self):
        manager = self.manager(preset={"first": {}})
        self.run_async(runner.run_sequence(manager, "run"))
        self.assert_closed_once()

    def test_snapshot_success_closes_devices_after_collection_and_save(self):
        manager = self.manager()
        result = self.run_async(runner.take_snapshot(manager, "run"))
        self.assertEqual(result, {
            "first": {"position": 1.0}, "second": {"position": 1.0}
        })
        manager.save_snapshot.assert_called_once_with("run", result)
        self.assert_closed_once()

    def test_snapshot_read_failure_closes_every_device_and_preserves_error(self):
        failure = OSError("snapshot read failed")

        async def fail(device):
            raise failure

        manager = self.manager([{"name": "first", "snapshot": fail}, {"name": "second"}])
        with self.assertRaises(OSError) as caught:
            self.run_async(runner.take_snapshot(manager, "run"))
        self.assertIs(caught.exception, failure)
        manager.save_snapshot.assert_not_called()
        self.assert_closed_once()

    def test_snapshot_save_failure_still_closes_and_preserves_error(self):
        manager = self.manager()
        failure = OSError("snapshot disk failed")
        manager.save_snapshot.side_effect = failure
        with self.assertRaises(OSError) as caught:
            self.run_async(runner.take_snapshot(manager, "run"))
        self.assertIs(caught.exception, failure)
        self.assert_closed_once()

    def test_sequence_build_failure_closes_constructed_devices(self):
        manager = self.manager(preset={"missing": {}})
        with self.assertRaises(SequenceError):
            self.run_async(runner.run_sequence(manager, "run"))
        self.assert_closed_once()

    def test_unknown_preset_closes_devices_already_constructed(self):
        manager = self.manager()
        with self.assertRaisesRegex(ValueError, "not found"):
            self.run_async(runner.run_sequence(manager, "unknown"))
        self.assert_closed_once()

    def test_partial_construction_closes_earlier_and_incomplete_devices(self):
        failure = ValueError("constructor failed after driver allocation")
        manager = self.manager([
            {"name": "first"},
            {"name": "second", "construct_error": failure},
            {"name": "never"},
        ])
        with self.assertRaises(ValueError) as caught:
            self.run_async(runner.run_sequence(manager, "run"))
        self.assertIs(caught.exception, failure)
        self.assertEqual([device.name for device in self.created], ["first", "second"])
        self.assert_closed_once()

    def test_execution_and_cleanup_failures_preserve_primary_and_close_sibling(self):
        failure = ValueError("execution failed")

        async def fail(device, pose):
            raise failure

        manager = self.manager([
            {"name": "first", "process": fail, "close": OSError("cleanup failed")},
            {"name": "second"},
        ])
        with self.assertLogs(level="ERROR"), self.assertRaises(MoveError) as caught:
            self.run_async(runner.run_sequence(manager, "run"))
        self.assertIs(caught.exception.__cause__, failure)
        self.assert_closed_once()
        self.created[0].driver.close.side_effect = None
        self.run_async(self.created[0].close())

    def test_cleanup_only_failure_is_reported_after_every_device_was_attempted(self):
        manager = self.manager([
            {"name": "first", "close": OSError("driver close failed")},
            {"name": "second"},
        ])
        with self.assertLogs(level="ERROR"), self.assertRaises(CleanupError):
            self.run_async(runner.run_sequence(manager, "run"))
        self.assert_closed_once()
        self.created[0].driver.close.side_effect = None
        self.run_async(self.created[0].close())

    def test_repeated_runner_iterations_dispose_before_next_construction(self):
        manager = self.manager()
        completed = []

        def after_iteration(iteration, count):
            for device in self.created:
                device.driver.close.assert_called_once_with()
            completed.append(iteration)

        self.run_async(runner.run_sequences(
            manager, "run", loop_count=3, on_iteration_end=after_iteration
        ))
        self.assertEqual(completed, [1, 2, 3])
        self.assertEqual(len(self.created), 6)
        self.assert_closed_once()

    def test_run_device_preset_only_constructs_and_closes_selected_device(self):
        manager = self.manager()
        self.run_async(runner.run_device_preset(manager, "second", "run"))
        self.assertEqual([device.name for device in self.created], ["second"])
        self.assert_closed_once()

    def test_invalid_device_selection_does_not_allocate_resources(self):
        manager = self.manager()
        with self.assertRaisesRegex(ValueError, "no entry"):
            self.run_async(runner.run_device_preset(manager, "missing", "run"))
        self.assertEqual(self.created, [])

    def test_repeated_close_and_default_legacy_close_remain_compatible(self):
        device = ResourceDevice({"name": "owned"})
        legacy = LegacyDevice()

        async def scenario():
            await asyncio.gather(device.close(), device.close())
            await device.close()
            await legacy.close()
            await legacy.close()

        self.run_async(scenario())
        device.driver.close.assert_called_once_with()

    def test_direct_scheduler_does_not_close_caller_owned_device(self):
        device = ResourceDevice({"name": "caller"})
        self.run_async(Scheduler([MoveStep(device, {}, 0)]).run())
        device.driver.close.assert_not_called()
        self.run_async(device.close())
        device.driver.close.assert_called_once_with()

    def test_runner_cancellation_stops_then_closes(self):
        async def scenario():
            started = asyncio.Event()
            stopped = asyncio.Event()

            async def process(device, pose):
                started.set()
                await stopped.wait()

            async def stop(device):
                stopped.set()

            manager = self.manager([{"name": "first", "process": process, "stop": stop}])
            running = asyncio.create_task(runner.run_sequence(manager, "run"))
            await asyncio.wait_for(started.wait(), 1)
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(running, 1)
            self.assertTrue(stopped.is_set())
            self.assert_closed_once()

        self.run_async(scenario())

    def test_repeated_cancellation_during_close_does_not_abandon_disposal(self):
        release = threading.Event()

        async def scenario():
            closing = asyncio.Event()
            loop = asyncio.get_running_loop()

            def close():
                loop.call_soon_threadsafe(closing.set)
                release.wait(2)

            manager = self.manager([{"name": "first", "close": close}])
            running = asyncio.create_task(runner.run_sequence(manager, "run"))
            try:
                await asyncio.wait_for(closing.wait(), 1)
                running.cancel()
                await asyncio.sleep(0)
                running.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(running, 1)
                self.assert_closed_once()
                self.assertNotIn(self.created[0], retained_devices())
            finally:
                release.set()

        self.run_async(scenario())

    def test_cancelled_executor_waiter_keeps_resources_until_worker_finishes(self):
        release = threading.Event()

        async def scenario():
            device = ResourceDevice({"name": "worker"})
            started = asyncio.Event()
            loop = asyncio.get_running_loop()

            def work():
                loop.call_soon_threadsafe(started.set)
                release.wait(2)

            running = asyncio.create_task(run_blocking(device, work))
            try:
                await asyncio.wait_for(started.wait(), 1)
                running.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await running
                with self.assertLogs(level="ERROR"), self.assertRaises(CleanupError):
                    async with DeviceScope(timeout=0.01):
                        own_device(device)
                device.driver.close.assert_not_called()
                self.assertIn(device, retained_devices())
                release.set()
                await asyncio.wait_for(device.close(), 1)
                device.driver.close.assert_called_once_with()
            finally:
                release.set()

        self.run_async(scenario())

    def test_snapshot_failure_protects_sibling_executor_read_until_it_finishes(self):
        release = threading.Event()
        failure = OSError("one read failed")

        async def scenario():
            started = asyncio.Event()
            loop = asyncio.get_running_loop()

            def read():
                loop.call_soon_threadsafe(started.set)
                release.wait(2)
                return {"position": 3.0}

            async def slow(device):
                return await run_blocking(device, read)

            async def fail(device):
                await started.wait()
                raise failure

            manager = self.manager([
                {"name": "first", "snapshot": slow},
                {"name": "second", "snapshot": fail},
            ])
            with patch.object(runner, "DeviceScope", side_effect=lambda: DeviceScope(timeout=0.01)):
                running = asyncio.create_task(runner.take_snapshot(manager, "run"))
                try:
                    with self.assertLogs(level="ERROR"), self.assertRaises(OSError) as caught:
                        await asyncio.wait_for(running, 1)
                    self.assertIs(caught.exception, failure)
                    self.created[0].driver.close.assert_not_called()
                    self.created[1].driver.close.assert_called_once_with()
                    release.set()
                    await asyncio.wait_for(self.created[0].close(), 1)
                    self.assert_closed_once()
                finally:
                    release.set()

        self.run_async(scenario())

    def test_external_close_override_cannot_bypass_unconfirmed_retention(self):
        device = LegacyDevice()
        device.close = AsyncMock()
        mark_unconfirmed(device, "original operation did not settle naturally")

        async def scenario():
            with self.assertLogs(level="ERROR"), self.assertRaises(CleanupError):
                async with DeviceScope(timeout=0.01):
                    own_device(device)
            device.close.assert_not_awaited()
            self.assertIn(device, retained_devices())

        # The retained extension has no actual resources; isolate this deliberate
        # permanent-retention case from other tests' process-wide diagnostics.
        with patch.object(lifecycle_module, "_context", lifecycle_module.LifecycleContext()):
            self.run_async(scenario())

    def test_hold_is_requested_explicitly_for_adapters_that_track_their_work(self):
        async def scenario():
            # Unconfirmed alone: closed once the busy state reads idle.
            settled = ResourceDevice({"name": "settled"})
            mark_unconfirmed(settled, "original operation did not settle naturally")
            await asyncio.wait_for(settled.close(), 1)
            settled.driver.close.assert_called_once_with()

            held = ResourceDevice({"name": "held"})
            mark_unconfirmed(held, "stop request failed", hold=True)
            with self.assertLogs(level="ERROR"), self.assertRaisesRegex(
                CleanupError, "held retained: stop request failed"
            ):
                await asyncio.wait_for(held.close(), 1)
            held.driver.close.assert_not_called()
            self.assertIn(held, retained_devices())

            # An extension that does not declare tracked work is always held.
            legacy = LegacyDevice()
            mark_unconfirmed(legacy, "original operation did not settle naturally")
            with self.assertLogs(level="ERROR"), self.assertRaisesRegex(
                CleanupError, "legacy retained"
            ):
                await asyncio.wait_for(legacy.close(), 1)

        with patch.object(lifecycle_module, "_context", lifecycle_module.LifecycleContext()):
            self.run_async(scenario())

    def test_cancelled_close_waiter_reuses_in_progress_disposal(self):
        release = threading.Event()

        async def scenario():
            started = asyncio.Event()
            loop = asyncio.get_running_loop()

            def close():
                loop.call_soon_threadsafe(started.set)
                release.wait(2)

            device = ResourceDevice({"name": "worker", "close": close})
            closing = asyncio.create_task(device.close())
            try:
                await asyncio.wait_for(started.wait(), 1)
                with self.assertRaisesRegex(RuntimeError, "closing"):
                    ensure_open(device)
                closing.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await closing
                again = asyncio.create_task(device.close())
                await asyncio.sleep(0)
                device.driver.close.assert_called_once_with()
                release.set()
                await asyncio.wait_for(again, 1)
                await device.close()
                device.driver.close.assert_called_once_with()
            finally:
                release.set()

        self.run_async(scenario())

    def test_cleanup_only_deadline_keeps_blocking_disposal_running(self):
        release = threading.Event()

        async def scenario():
            started = asyncio.Event()
            loop = asyncio.get_running_loop()

            def close():
                loop.call_soon_threadsafe(started.set)
                release.wait(2)

            device = ResourceDevice({"name": "slow-close", "close": close})
            try:
                with self.assertLogs(level="ERROR"), self.assertRaisesRegex(
                    CleanupError, "deferred"
                ):
                    async with DeviceScope(timeout=0.01):
                        own_device(device)
                self.assertTrue(started.is_set())
                self.assertIn(device, retained_devices())
                device.driver.close.assert_called_once_with()
                release.set()
                await asyncio.wait_for(device.close(), 1)
                self.assertNotIn(device, retained_devices())
                device.driver.close.assert_called_once_with()
            finally:
                release.set()

        self.run_async(scenario())

    def test_unconfirmed_external_worker_is_retained_even_after_idle_return(self):
        release = threading.Event()

        async def scenario():
            started = asyncio.Event()
            loop = asyncio.get_running_loop()
            device = LegacyDevice()
            resource = Mock()
            track_resource(device, resource, resource.close)

            def work():
                loop.call_soon_threadsafe(started.set)
                release.wait(2)

            running = asyncio.create_task(run_blocking(device, work))
            try:
                await asyncio.wait_for(started.wait(), 1)
                running.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await running
                mark_unconfirmed(device, "original operation did not settle naturally")
                with self.assertLogs(level="ERROR"), self.assertRaises(CleanupError):
                    async with DeviceScope(timeout=0.01):
                        own_device(device)
                resource.close.assert_not_called()
                self.assertFalse(device.busy)
                release.set()
                with self.assertLogs(level="ERROR"), self.assertRaises(CleanupError):
                    await asyncio.wait_for(device.close(), 1)
                pending = [task for task in lifecycle_module.lifecycle_context().background if task.get_loop() is loop]
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                resource.close.assert_not_called()
                self.assertIn(device, retained_devices())
            finally:
                release.set()

        with patch.object(lifecycle_module, "_context", lifecycle_module.LifecycleContext()):
            self.run_async(scenario())
