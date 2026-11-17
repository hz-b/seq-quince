"""Cached readiness must follow native workers beyond waiter cancellation."""

import asyncio
import threading
from unittest.mock import Mock

from seq_quince.device import lifecycle
from seq_quince.device.device_abs import DeviceABC
from seq_quince.device.lifecycle import (
    CleanupError,
    MotionBusyMixin,
    MotionState,
    has_pending_work,
    mark_unconfirmed,
    run_blocking,
    run_operation,
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


class GatedChecker:
    """Checker that reads hardware first, then blocks until released."""

    def __init__(self, loop, moving):
        self.loop = loop
        self.moving = moving
        self.gate = threading.Event()
        self.sampled = asyncio.Event()
        self.calls = 0
        self.running = 0
        self.overlap = 0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.calls += 1
            self.running += 1
            self.overlap = max(self.overlap, self.running)
        try:
            value = self.moving.is_set()
            self.loop.call_soon_threadsafe(self.sampled.set)
            if not self.gate.wait(2):
                raise TimeoutError("test checker not released")
            return value
        finally:
            with self._lock:
                self.running -= 1


class RefreshSamplingTests(CoreTestCase):
    def setUp(self):
        super().setUp()
        self.moving = threading.Event()
        self.device = TrackedMotionDevice()
        self.device.motion = MotionState(self.device, self.moving.is_set)
        self.resource = Mock()
        track_resource(self.device, self.resource, self.resource.close)

    async def _wait_idle(self):
        while self.device.busy:
            await asyncio.sleep(0.005)

    def test_refresh_rejects_idle_when_pending_worker_moves_during_sample(self):
        release = threading.Event()

        async def scenario():
            loop = asyncio.get_running_loop()
            checker = GatedChecker(loop, self.moving)
            self.device.motion.checker = checker
            started = asyncio.Event()

            def work():
                loop.call_soon_threadsafe(started.set)
                if not release.wait(2):
                    raise TimeoutError("test worker not released")
                self.moving.set()

            running = asyncio.create_task(run_blocking(self.device, work))
            try:
                await asyncio.wait_for(started.wait(), 1)
                worker = self.device._resource_lifecycle.tracker.commands()
                running.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await running
                refresh = asyncio.create_task(self.device.motion.refresh())
                await asyncio.wait_for(checker.sampled.wait(), 1)  # idle read
                release.set()
                await asyncio.wait_for(asyncio.wait(worker), 1)
                await asyncio.sleep(0)  # let done callbacks drop the worker
                checker.gate.set()
                await asyncio.wait_for(refresh, 1)
                self.assertTrue(self.device.motion.busy)
                self.moving.clear()
                await asyncio.wait_for(self._wait_idle(), 1)
            finally:
                release.set()
                checker.gate.set()
                self.moving.clear()
                await asyncio.wait_for(self.device.close(), 1)

        self.run_async(scenario())

    def test_refresh_rejects_idle_when_command_completes_within_sample(self):
        async def scenario():
            loop = asyncio.get_running_loop()
            checker = GatedChecker(loop, self.moving)
            self.device.motion.checker = checker
            try:
                refresh = asyncio.create_task(self.device.motion.refresh())
                await asyncio.wait_for(checker.sampled.wait(), 1)  # idle read
                # Neither endpoint sees pending work; only the generation does.
                await run_blocking(self.device, self.moving.set)
                checker.gate.set()
                await asyncio.wait_for(refresh, 1)
                self.assertTrue(self.device.motion.busy)
                self.moving.clear()
                await asyncio.wait_for(self._wait_idle(), 1)
            finally:
                checker.gate.set()
                self.moving.clear()
                await asyncio.wait_for(self.device.close(), 1)

        self.run_async(scenario())

    def test_observer_probe_does_not_invalidate_refresh(self):
        async def scenario():
            loop = asyncio.get_running_loop()
            checker = GatedChecker(loop, self.moving)
            self.device.motion.checker = checker
            try:
                self.device.motion.watch()
                await asyncio.wait_for(checker.sampled.wait(), 1)
                refresh = asyncio.create_task(self.device.motion.refresh())
                await asyncio.sleep(0.01)
                self.assertFalse(refresh.done())
                checker.gate.set()
                await asyncio.wait_for(refresh, 1)
                self.assertFalse(self.device.busy)
                self.assertEqual(checker.overlap, 1)
            finally:
                checker.gate.set()
                await asyncio.wait_for(self.device.close(), 1)

        self.run_async(scenario())

    def test_cancelled_refresh_keeps_checker_tracked_without_overlap(self):
        async def scenario():
            loop = asyncio.get_running_loop()
            checker = GatedChecker(loop, self.moving)
            self.device.motion.checker = checker
            closing = None
            try:
                first = asyncio.create_task(self.device.motion.refresh())
                await asyncio.wait_for(checker.sampled.wait(), 1)
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                self.assertTrue(self.device.motion.busy)
                second = asyncio.create_task(self.device.motion.refresh())
                closing = asyncio.create_task(self.device.close())
                await asyncio.sleep(0.01)
                self.assertFalse(second.done())
                self.assertFalse(closing.done())
                self.resource.close.assert_not_called()
                checker.gate.set()
                await asyncio.wait_for(second, 1)
                await asyncio.wait_for(closing, 1)
                self.assertEqual(checker.overlap, 1)
                self.resource.close.assert_called_once_with()
            finally:
                checker.gate.set()
                if closing is not None:
                    await asyncio.wait_for(closing, 1)

        self.run_async(scenario())


class MixinMotionDevice(MotionBusyMixin, DeviceABC):
    """Smallest adapter shape: MotionState in ``_motion``, busy from the mixin."""

    name = "mixin motion"
    _cleanup_tracks_work = True

    def __init__(self, checker):
        self._motion = MotionState(self, checker)

    @classmethod
    def from_config_dict(cls, config):
        return cls(lambda: False)

    async def process(self, pose):
        pass

    async def snapshot(self):
        return {}

    async def stop(self):
        pass


class MotionCommandTests(CoreTestCase):
    def setUp(self):
        super().setUp()
        self.moving = threading.Event()
        self.samples = []

        def checker():
            self.samples.append(threading.get_ident())
            return self.moving.is_set()

        self.device = MixinMotionDevice(checker)
        self.resource = Mock()
        track_resource(self.device, self.resource, self.resource.close)

    async def _wait_idle(self):
        while self.device.busy:
            await asyncio.sleep(0.005)

    def test_mixin_implements_busy_from_motion_state_and_pending_work(self):
        self.assertIs(type(self.device).busy, MotionBusyMixin.busy)
        self.assertTrue(self.device.busy)  # Unknown before the first sample.

        async def scenario():
            await self.device._motion.refresh()
            self.assertFalse(self.device.busy)
            self.assertEqual(len(self.samples), 1)
            self.assertFalse(self.device.busy)  # Reading never samples hardware.
            self.assertEqual(len(self.samples), 1)
            await self.device.close()

        self.run_async(scenario())

    def test_command_runs_in_worker_returns_result_and_confirms_idle(self):
        async def scenario():
            loop_thread = threading.get_ident()
            await self.device._motion.refresh()
            self.assertFalse(self.device.busy)
            seen = []

            def move(first, second):
                # Outstanding work alone keeps the device busy: no local flag.
                seen.append((threading.get_ident(), self.device.busy))
                return first + second

            result = await self.device._motion.command(move, 2, 3)
            self.assertEqual(result, 5)
            self.assertEqual(len(seen), 1)
            self.assertNotEqual(seen[0][0], loop_thread)
            self.assertTrue(seen[0][1])
            self.assertEqual(len(self.samples), 2)  # One sample per command.
            self.assertFalse(self.device.busy)
            await self.device.close()

        self.run_async(scenario())

    def test_device_stays_busy_between_a_finished_command_and_its_sample(self):
        release = threading.Event()

        async def scenario():
            loop = asyncio.get_running_loop()
            await self.device._motion.refresh()
            self.assertFalse(self.device.busy)
            started = asyncio.Event()
            seen = []

            def put():
                # Like a command that returns before the motion it starts.
                loop.call_soon_threadsafe(started.set)
                if not release.wait(2):
                    raise TimeoutError("test worker not released")

            running = asyncio.create_task(self.device._motion.command(put))
            try:
                await asyncio.wait_for(started.wait(), 1)
                (worker,) = self.device._resource_lifecycle.tracker.commands()
                # Runs once the worker is finished and before command() resumes.
                worker.add_done_callback(lambda done: seen.append(self.device.busy))
                release.set()
                await asyncio.wait_for(running, 1)
                self.assertEqual(seen, [True])
                self.assertFalse(self.device.busy)
            finally:
                release.set()
                await asyncio.wait_for(self.device.close(), 1)

        self.run_async(scenario())

    def test_failed_command_propagates_and_stays_busy_until_observed_idle(self):
        async def scenario():
            checker = GatedChecker(asyncio.get_running_loop(), self.moving)
            self.device._motion.checker = checker
            error = RuntimeError("drive fault")

            def move():
                raise error

            try:
                with self.assertRaises(RuntimeError) as caught:
                    await self.device._motion.command(move)
                self.assertIs(caught.exception, error)
                # The observer took over; its first probe is still in flight.
                await asyncio.wait_for(checker.sampled.wait(), 1)
                self.assertTrue(self.device.busy)
                checker.gate.set()
                await asyncio.wait_for(self._wait_idle(), 1)
                self.assertEqual(checker.overlap, 1)
            finally:
                checker.gate.set()
                await asyncio.wait_for(self.device.close(), 1)

        self.run_async(scenario())

    def test_commands_block_samples_once_after_all_commands(self):
        async def scenario():
            order = []
            async with self.device._motion.commands():
                await run_blocking(self.device, order.append, "first")
                self.assertEqual(self.samples, [])
                await run_blocking(self.device, order.append, "second")
                self.assertEqual(self.samples, [])
                self.assertTrue(self.device.busy)
            self.assertEqual(order, ["first", "second"])
            self.assertEqual(len(self.samples), 1)
            self.assertFalse(self.device.busy)
            await self.device.close()

        self.run_async(scenario())

    def test_failed_commands_block_skips_the_final_sample_and_is_observed(self):
        async def scenario():
            checker = GatedChecker(asyncio.get_running_loop(), self.moving)
            self.device._motion.checker = checker
            try:
                with self.assertRaisesRegex(ValueError, "stopped"):
                    async with self.device._motion.commands():
                        await run_blocking(self.device, lambda: None)
                        raise ValueError("stopped")
                await asyncio.wait_for(checker.sampled.wait(), 1)
                self.assertTrue(self.device.busy)
                checker.gate.set()
                await asyncio.wait_for(self._wait_idle(), 1)
                self.assertEqual(checker.overlap, 1)
            finally:
                checker.gate.set()
                await asyncio.wait_for(self.device.close(), 1)

        self.run_async(scenario())

    def test_cancelled_command_keeps_worker_visible_until_it_finishes(self):
        release = threading.Event()

        async def scenario():
            loop = asyncio.get_running_loop()
            started = asyncio.Event()

            def move():
                loop.call_soon_threadsafe(started.set)
                if not release.wait(2):
                    raise TimeoutError("test worker not released")

            running = asyncio.create_task(self.device._motion.command(move))
            closing = None
            try:
                await asyncio.wait_for(started.wait(), 1)
                running.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await running
                self.assertTrue(has_pending_work(self.device))
                self.assertTrue(self.device.busy)
                closing = asyncio.create_task(self.device.close())
                await asyncio.sleep(0)
                self.resource.close.assert_not_called()
                release.set()
                await asyncio.wait_for(closing, 1)
                self.assertFalse(self.device._motion.busy)
                self.resource.close.assert_called_once_with()
            finally:
                release.set()
                if closing is not None:
                    await asyncio.wait_for(closing, 1)

        self.run_async(scenario())


class SingleSamplerTests(CoreTestCase):
    """The observer takes every sample; refresh() only asks and waits."""

    def setUp(self):
        super().setUp()
        self.patch(lifecycle, "_context", lifecycle.LifecycleContext())
        self.moving = threading.Event()
        self.samples = []
        self.errors = []

        def checker():
            self.samples.append(self.moving.is_set())
            if self.errors:
                raise self.errors.pop(0)
            return self.samples[-1]

        self.device = MixinMotionDevice(checker)
        self.motion = self.device._motion
        self.resource = Mock()
        track_resource(self.device, self.resource, self.resource.close)

    async def _observer_finished(self):
        await asyncio.wait({self.motion._observer}, timeout=1)
        self.assertTrue(self.motion._observer.done())

    def test_refresh_is_answered_by_a_sample_that_starts_after_the_call(self):
        async def scenario():
            checker = GatedChecker(asyncio.get_running_loop(), self.moving)
            self.motion.checker = checker
            try:
                self.motion.watch()
                await asyncio.wait_for(checker.sampled.wait(), 1)  # idle read
                # Motion that no tracked command announced, e.g. another client.
                self.moving.set()
                refresh = asyncio.create_task(self.motion.refresh())
                await asyncio.sleep(0)
                checker.gate.set()
                await asyncio.wait_for(refresh, 1)
                # The idle read from before the call did not answer it.
                self.assertTrue(self.motion.busy)
                self.assertEqual(checker.calls, 2)
                self.assertEqual(checker.overlap, 1)
            finally:
                checker.gate.set()
                self.moving.clear()
                await asyncio.wait_for(self.device.close(), 1)

        self.run_async(scenario())

    def test_one_sample_answers_every_waiting_refresh_call(self):
        async def scenario():
            await asyncio.gather(*(self.motion.refresh() for _ in range(3)))
            self.assertEqual(self.samples, [False])
            self.assertFalse(self.device.busy)
            await self._observer_finished()  # Idle and nobody waiting: it ends.
            await self.device.close()

        self.run_async(scenario())

    def test_refresh_ends_the_polling_pause(self):
        self.patch(lifecycle, "_POLL_INTERVAL", 30)

        async def scenario():
            self.moving.set()
            await asyncio.wait_for(self.motion.refresh(), 1)
            self.assertTrue(self.device.busy)
            self.moving.clear()
            # The observer is pausing; this call must not wait for the interval.
            await asyncio.wait_for(self.motion.refresh(), 1)
            self.assertFalse(self.device.busy)
            self.assertEqual(self.samples, [True, False])
            await asyncio.wait_for(self.device.close(), 1)

        self.run_async(scenario())

    def test_completed_command_ends_the_polling_pause(self):
        self.patch(lifecycle, "_POLL_INTERVAL", 30)

        async def scenario():
            async def snapshot():
                # As in a scheduler snapshot: the sample runs inside a tracked
                # operation and therefore cannot confirm idle on its own.
                await self.motion.refresh()
                self.assertTrue(self.device.busy)

            await run_operation(self.device, snapshot())
            await self._observer_finished()
            self.assertFalse(self.device.busy)
            self.assertEqual(self.samples, [False, False])
            await asyncio.wait_for(self.device.close(), 1)

        self.run_async(scenario())

    def test_sampling_error_reaches_refresh_and_recovery_leaves_no_hold(self):
        error = OSError("readback unavailable")
        self.errors.append(error)

        async def scenario():
            # assertRaises also clears the frames of the traceback it caught.
            # Before Python 3.13 that ends a coroutine suspended in one of them,
            # so the error must not lead back to the running observer.
            with self.assertRaises(OSError) as caught:
                await self.motion.refresh()
            self.assertIs(caught.exception, error)
            self.assertTrue(self.device.busy)
            # The observer kept going on its own and confirmed idle.
            await self._observer_finished()
            self.assertEqual(len(self.samples), 2)
            self.assertFalse(self.device.busy)
            await asyncio.wait_for(self.device.close(), 1)

        self.run_async(scenario())
        self.resource.close.assert_called_once_with()

    def test_persistent_sampling_error_leaves_the_device_unconfirmed(self):
        self.errors.extend(OSError("readback unavailable") for _ in range(2))

        async def scenario():
            with self.assertRaises(OSError):
                await self.motion.refresh()
            await self._observer_finished()
            self.assertIsInstance(self.motion._observer.exception(), OSError)
            self.assertEqual(len(self.samples), 2)
            self.assertTrue(self.device.busy)
            with self.assertLogs(level="ERROR"), self.assertRaisesRegex(
                CleanupError, "busy state unavailable"
            ):
                await asyncio.wait_for(self.device.close(), 1)

        self.run_async(scenario())
        self.resource.close.assert_not_called()

    def test_cancelled_observer_does_not_leave_refresh_waiting(self):
        async def scenario():
            checker = GatedChecker(asyncio.get_running_loop(), self.moving)
            self.motion.checker = checker
            refresh = asyncio.create_task(self.motion.refresh())
            try:
                await asyncio.wait_for(checker.sampled.wait(), 1)
                self.motion._observer.cancel()
                done, _ = await asyncio.wait({refresh}, timeout=1)
                self.assertTrue(done)
                self.assertTrue(refresh.cancelled())
                self.assertTrue(self.device.busy)
            finally:
                checker.gate.set()
                with self.assertLogs(level="ERROR"), self.assertRaisesRegex(
                    CleanupError, "busy state unavailable"
                ):
                    await asyncio.wait_for(self.device.close(), 1)

        self.run_async(scenario())
