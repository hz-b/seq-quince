"""Tracked runner workers keep construction and snapshot I/O off the loop."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

from seq_quince import runner
from seq_quince.device.lifecycle import track_resource
from seq_quince.device_concrete.sim import SimDevice
from seq_quince.sequence import MoveStep

from ._support import CoreTestCase


def selected_manager():
    return SimpleNamespace(
        get_device_configs=lambda: [{"name": "axis", "type": "SimDevice"}],
        presets={"run": {"axis": {"position": 2.0}}},
        replacements={},
    )


class RunnerExecutorTests(CoreTestCase):
    def test_single_device_construction_does_not_block_event_loop(self):
        release = threading.Event()
        device = SimDevice("axis", delay=0)

        async def scenario():
            started = asyncio.Event()
            loop = asyncio.get_running_loop()
            loop_thread = threading.get_ident()

            def create(config):
                self.assertNotEqual(threading.get_ident(), loop_thread)
                loop.call_soon_threadsafe(started.set)
                if not release.wait(2):
                    raise TimeoutError("test constructor was not released")
                return device

            with patch.object(runner.DeviceFactory, "create", side_effect=create):
                running = asyncio.create_task(
                    runner.run_device_preset(selected_manager(), "axis", "run")
                )
                try:
                    await asyncio.wait_for(started.wait(), 1)
                    # This coroutine can run while the constructor is blocked.
                    self.assertFalse(running.done())
                    self.assertFalse(release.is_set())
                    release.set()
                    await asyncio.wait_for(running, 1)
                finally:
                    release.set()
                    await asyncio.gather(running, return_exceptions=True)

        self.run_async(scenario())

    def test_cancellation_waits_for_construction_before_disposing_late_resources(self):
        release = threading.Event()
        first, late = Mock(), Mock()

        async def scenario():
            started = asyncio.Event()
            loop = asyncio.get_running_loop()

            def create(config):
                device = SimDevice("axis", delay=0)
                track_resource(device, first, first.close)
                loop.call_soon_threadsafe(started.set)
                if not release.wait(2):
                    raise TimeoutError("test constructor was not released")
                # Cancellation may already have started scope disposal here.
                track_resource(device, late, late.close)
                return device

            with patch.object(runner.DeviceFactory, "create", side_effect=create):
                running = asyncio.create_task(
                    runner.run_device_preset(selected_manager(), "axis", "run")
                )
                try:
                    await asyncio.wait_for(started.wait(), 1)
                    running.cancel()
                    await asyncio.sleep(0.01)
                    self.assertFalse(running.done())
                    first.close.assert_not_called()
                    late.close.assert_not_called()
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(running, 1)
                finally:
                    release.set()
                    await asyncio.gather(running, return_exceptions=True)
            first.close.assert_called_once_with()
            late.close.assert_called_once_with()

        self.run_async(scenario())

    def test_worker_construction_failure_disposes_partial_resources(self):
        resource = Mock()
        failure = ValueError("constructor failed after allocating resource")

        def create(config):
            device = SimDevice("axis", delay=0)
            track_resource(device, resource, resource.close)
            raise failure

        with patch.object(
            runner.DeviceFactory, "create", side_effect=create
        ), self.assertRaises(ValueError) as caught:
            self.run_async(runner.run_device_preset(selected_manager(), "axis", "run"))
        self.assertIs(caught.exception, failure)
        resource.close.assert_called_once_with()

    def test_scheduler_construction_and_snapshot_save_run_in_workers_in_order(self):
        release = threading.Event()
        device = SimDevice("axis", {"position": 1.0}, delay=0)
        events = []
        loop_thread = threading.get_ident()
        manager = Mock()

        def create_devices(factory):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            events.append("construct")
            return {"axis": device}

        def build(preset, devices, default_grouping):
            events.append("build")
            return [MoveStep(devices["axis"], {}, 0)]

        manager.create_devices.side_effect = create_devices
        manager.build_sequence_from_preset.side_effect = build

        async def scenario():
            started = asyncio.Event()
            loop = asyncio.get_running_loop()

            def save(preset, snapshot):
                self.assertNotEqual(threading.get_ident(), loop_thread)
                self.assertEqual(snapshot, {"axis": {"position": 1.0}})
                events.append("save")
                loop.call_soon_threadsafe(started.set)
                if not release.wait(2):
                    raise TimeoutError("test snapshot save was not released")

            manager.save_snapshot.side_effect = save
            taking = asyncio.create_task(runner.take_snapshot(manager, "saved"))
            try:
                await asyncio.wait_for(started.wait(), 1)
                self.assertEqual(events, ["construct", "build", "save"])
                self.assertFalse(taking.done())
                release.set()
                result = await asyncio.wait_for(taking, 1)
                self.assertEqual(result, {"axis": {"position": 1.0}})
            finally:
                release.set()
                await asyncio.gather(taking, return_exceptions=True)

        self.run_async(scenario())
