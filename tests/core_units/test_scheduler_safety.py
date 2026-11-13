"""Regression tests for stopping and settling operations before rollback."""

import asyncio
import threading
from unittest.mock import AsyncMock, Mock, call

from seq_quince.scheduler import MoveError, Scheduler
from seq_quince.sequence import MoveStep

from ._support import CoreTestCase


class Device:
    """Inspectable device contract without Channel Access or real motion."""

    def __init__(self, name, initial=0, rollback=True):
        self.name = name
        self.busy = False
        self.snapshot = AsyncMock(return_value={"position": initial})
        self.process = AsyncMock()
        self.stop = AsyncMock()
        self.scheduler_timeout = Mock(return_value=None)
        self.supports_rollback = Mock(return_value=rollback)


class SchedulerSafetyTests(CoreTestCase):
    def test_parallel_failure_stops_and_settles_sibling_before_rollback(self):
        prior, active, broken, later = [
            Device(name) for name in ("prior", "active", "broken", "later")
        ]
        failure = RuntimeError("parallel drive failed")
        events = []

        async def scenario():
            started = asyncio.Event()
            stop_requested = asyncio.Event()
            settled = asyncio.Event()

            async def move_active(pose):
                active.busy = True
                started.set()
                try:
                    await stop_requested.wait()
                    events.append("active settled")
                    raise RuntimeError("stopped")
                finally:
                    active.busy = False
                    settled.set()

            async def fail(pose):
                await started.wait()
                raise failure

            async def stop_active():
                events.append("stop active")
                stop_requested.set()

            async def move_prior(pose):
                if pose == {"position": 0}:
                    self.assertTrue(settled.is_set())
                    self.assertFalse(active.busy)
                    events.append("rollback prior")

            active.process.side_effect = move_active
            active.stop.side_effect = stop_active
            broken.process.side_effect = fail
            prior.process.side_effect = move_prior
            scheduler = Scheduler([
                MoveStep(prior, {"position": 1}, 0),
                MoveStep(active, {"position": 2}, 1),
                MoveStep(broken, {}, 1),
                MoveStep(later, {}, 2),
            ], cleanup_timeout=0.1)
            with self.assertLogs("seq_quince.scheduler", level="ERROR"), self.assertRaises(MoveError) as caught:
                await asyncio.wait_for(scheduler.run(), 1)
            self.assertIs(caught.exception.__cause__, failure)
            active.stop.assert_awaited_once_with()
            later.process.assert_not_awaited()
            later.snapshot.assert_not_awaited()
            self.assertEqual(events, ["stop active", "active settled", "rollback prior"])

        self.run_async(scenario())

    def test_completed_steps_in_failing_group_are_rolled_back_if_supported(self):
        completed = Device("completed", initial=10)
        unsupported = Device("unsupported", rollback=False)
        broken = Device("broken")
        failure = ValueError("bad target")

        async def scenario():
            finished = asyncio.Event()
            done = set()

            async def finish(name):
                done.add(name)
                if len(done) == 2:
                    finished.set()

            async def complete(pose):
                if pose == {"position": 1}:
                    await finish("completed")

            async def no_rollback(pose):
                await finish("unsupported")

            async def fail(pose):
                await finished.wait()
                raise failure

            completed.process.side_effect = complete
            unsupported.process.side_effect = no_rollback
            broken.process.side_effect = fail
            scheduler = Scheduler([
                MoveStep(completed, {"position": 1}, 0),
                MoveStep(unsupported, {"position": 2}, 0),
                MoveStep(broken, {}, 0),
            ])
            with self.assertLogs("seq_quince.scheduler", level="ERROR"), self.assertRaises(MoveError) as caught:
                await scheduler.run()
            self.assertIs(caught.exception.__cause__, failure)
            self.assertEqual(completed.process.await_args_list, [
                call({"position": 1}), call({"position": 10}),
            ])
            unsupported.process.assert_awaited_once_with({"position": 2})
            unsupported.snapshot.assert_not_awaited()

        self.run_async(scenario())

    def test_cancellation_stops_active_group_and_prevents_next_group(self):
        prior, active, later = [Device(name) for name in ("prior", "active", "later")]

        async def scenario():
            started = asyncio.Event()
            stopped = asyncio.Event()
            settled = asyncio.Event()

            async def process(pose):
                active.busy = True
                started.set()
                try:
                    await stopped.wait()
                    raise RuntimeError("stopped by scheduler")
                finally:
                    active.busy = False
                    settled.set()

            async def stop():
                stopped.set()

            async def rollback(pose):
                if pose == {"position": 0}:
                    self.assertTrue(settled.is_set())

            active.process.side_effect = process
            active.stop.side_effect = stop
            prior.process.side_effect = rollback
            scheduler = Scheduler([
                MoveStep(prior, {"position": 1}, 0),
                MoveStep(active, {"position": 2}, 1),
                MoveStep(later, {}, 2),
            ], cleanup_timeout=0.1)
            running = asyncio.create_task(scheduler.run())
            await asyncio.wait_for(started.wait(), 1)
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(running, 1)
            active.stop.assert_awaited_once_with()
            self.assertTrue(settled.is_set())
            self.assertEqual(prior.process.await_args_list, [
                call({"position": 1}), call({"position": 0}),
            ])
            later.process.assert_not_awaited()
            later.snapshot.assert_not_awaited()

        self.run_async(scenario())

    def test_stop_failure_does_not_skip_another_active_device(self):
        first, second, broken = [Device(name) for name in ("first", "second", "broken")]
        failure = RuntimeError("original failure")

        async def scenario():
            started = set()
            both_started = asyncio.Event()
            release = asyncio.Event()

            async def move(name):
                started.add(name)
                if len(started) == 2:
                    both_started.set()
                await release.wait()
                raise RuntimeError("motion stopped")

            async def move_first(pose):
                await move("first")

            async def move_second(pose):
                await move("second")

            async def fail(pose):
                await both_started.wait()
                raise failure

            async def stop_second():
                release.set()

            first.process.side_effect = move_first
            second.process.side_effect = move_second
            broken.process.side_effect = fail
            first.stop.side_effect = OSError("stop failed")
            second.stop.side_effect = stop_second
            scheduler = Scheduler([
                MoveStep(first, {}, 0), MoveStep(second, {}, 0), MoveStep(broken, {}, 0),
            ], cleanup_timeout=0.1)
            with self.assertLogs("seq_quince.scheduler", level="ERROR") as logs, self.assertRaises(MoveError) as caught:
                await asyncio.wait_for(scheduler.run(), 1)
            self.assertIs(caught.exception.__cause__, failure)
            first.stop.assert_awaited_once_with()
            second.stop.assert_awaited_once_with()
            self.assertIn("first", "\n".join(logs.output))
            self.assertIn("stop failed", "\n".join(logs.output))

        self.run_async(scenario())

    def test_executor_operation_with_noop_stop_cannot_race_prior_rollback(self):
        motor, broken, later = [Device(name) for name in ("motor", "broken", "later")]
        failure = RuntimeError("parallel failure")
        release = threading.Event()
        worker_running = threading.Event()

        async def scenario():
            loop = asyncio.get_running_loop()
            started = asyncio.Event()
            worker_done = asyncio.Event()

            def hardware_move():
                worker_running.set()
                loop.call_soon_threadsafe(started.set)
                try:
                    release.wait()
                finally:
                    worker_running.clear()
                    loop.call_soon_threadsafe(worker_done.set)

            async def process(pose):
                if pose == {"position": 2}:
                    # busy deliberately stays False: task evidence must take precedence.
                    await loop.run_in_executor(None, hardware_move)

            async def fail(pose):
                await started.wait()
                raise failure

            motor.process.side_effect = process
            broken.process.side_effect = fail
            scheduler = Scheduler([
                MoveStep(motor, {"position": 1}, 0),
                MoveStep(motor, {"position": 2}, 1),
                MoveStep(broken, {}, 1),
                MoveStep(later, {}, 2),
            ], cleanup_timeout=0.01)
            try:
                with self.assertLogs("seq_quince.scheduler", level="ERROR") as logs, self.assertRaises(MoveError) as caught:
                    await asyncio.wait_for(scheduler.run(), 1)
                self.assertIs(caught.exception.__cause__, failure)
                self.assertTrue(worker_running.is_set())
                motor.stop.assert_awaited_once_with()
                self.assertEqual(motor.process.await_args_list, [
                    call({"position": 1}), call({"position": 2}),
                ])
                later.process.assert_not_awaited()
                self.assertIn("motor", "\n".join(logs.output))
            finally:
                release.set()
                await asyncio.wait_for(worker_done.wait(), 1)

        self.run_async(scenario())

    def test_stop_waits_for_executor_exit_before_rollback_on_same_device(self):
        motor, broken = Device("motor", initial=10), Device("broken")
        release = threading.Event()
        failure = RuntimeError("failed alongside motor")

        async def scenario():
            loop = asyncio.get_running_loop()
            started = asyncio.Event()
            worker_done = asyncio.Event()

            def hardware_move():
                loop.call_soon_threadsafe(started.set)
                try:
                    release.wait()
                finally:
                    loop.call_soon_threadsafe(worker_done.set)

            async def process(pose):
                if pose == {"position": 2}:
                    await loop.run_in_executor(None, hardware_move)
                if pose == {"position": 10}:
                    self.assertTrue(worker_done.is_set())

            async def stop():
                release.set()

            async def fail(pose):
                await started.wait()
                raise failure

            motor.process.side_effect = process
            motor.stop.side_effect = stop
            broken.process.side_effect = fail
            scheduler = Scheduler([
                MoveStep(motor, {"position": 1}, 0),
                MoveStep(motor, {"position": 2}, 1),
                MoveStep(broken, {}, 1),
            ], cleanup_timeout=0.1)
            try:
                with self.assertLogs("seq_quince.scheduler", level="ERROR"), self.assertRaises(MoveError) as caught:
                    await asyncio.wait_for(scheduler.run(), 1)
                self.assertIs(caught.exception.__cause__, failure)
                self.assertIn(call({"position": 10}), motor.process.await_args_list)
                motor.stop.assert_awaited_once_with()
            finally:
                release.set()
                await asyncio.wait_for(worker_done.wait(), 1)

        self.run_async(scenario())

    def test_repeated_cancellation_does_not_interrupt_stop_cleanup(self):
        active, later = Device("active"), Device("later")

        async def scenario():
            started = asyncio.Event()
            stopping = asyncio.Event()
            allow_stop = asyncio.Event()
            stopped = asyncio.Event()
            settled = asyncio.Event()

            async def process(pose):
                started.set()
                try:
                    await stopped.wait()
                    raise RuntimeError("stopped")
                finally:
                    settled.set()

            async def stop():
                stopping.set()
                await allow_stop.wait()
                stopped.set()

            active.process.side_effect = process
            active.stop.side_effect = stop
            scheduler = Scheduler([
                MoveStep(active, {}, 0), MoveStep(later, {}, 1),
            ], cleanup_timeout=0.1)
            running = asyncio.create_task(scheduler.run())
            try:
                await asyncio.wait_for(started.wait(), 1)
                running.cancel()
                await asyncio.wait_for(stopping.wait(), 1)
                running.cancel()
                allow_stop.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(running, 1)
                active.stop.assert_awaited_once_with()
                self.assertTrue(settled.is_set())
                later.process.assert_not_awaited()
            finally:
                allow_stop.set()
                stopped.set()

        self.run_async(scenario())

    def test_cleanup_is_bounded_even_if_stop_and_process_suppress_cancellation(self):
        active, broken = Device("active"), Device("broken")
        failure = RuntimeError("group failed")

        async def scenario():
            started = asyncio.Event()
            release = asyncio.Event()
            cleanup_tasks = []

            async def resist_cancellation():
                cleanup_tasks.append(asyncio.current_task())
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue

            async def process(pose):
                started.set()
                await resist_cancellation()

            async def fail(pose):
                await started.wait()
                raise failure

            active.process.side_effect = process
            active.stop.side_effect = resist_cancellation
            broken.process.side_effect = fail
            scheduler = Scheduler([
                MoveStep(active, {}, 0), MoveStep(broken, {}, 0),
            ], cleanup_timeout=0.01)
            try:
                with self.assertLogs("seq_quince.scheduler", level="ERROR") as logs, self.assertRaises(MoveError) as caught:
                    await asyncio.wait_for(scheduler.run(), 1)
                self.assertIs(caught.exception.__cause__, failure)
                active.stop.assert_awaited_once_with()
                self.assertIn("active", "\n".join(logs.output))
            finally:
                release.set()
                if cleanup_tasks:
                    await asyncio.wait_for(asyncio.gather(*cleanup_tasks), 1)

        self.run_async(scenario())

    def test_busy_device_after_process_failure_is_excluded_from_prior_rollback(self):
        motor = Device("motor")
        failure = RuntimeError("driver returned while another axis was active")

        async def process(pose):
            if pose == {"position": 2}:
                motor.busy = True
                raise failure

        motor.process.side_effect = process
        scheduler = Scheduler([
            MoveStep(motor, {"position": 1}, 0), MoveStep(motor, {"position": 2}, 1),
        ], cleanup_timeout=0.01)
        with self.assertLogs("seq_quince.scheduler", level="ERROR") as logs, self.assertRaises(MoveError) as caught:
            self.run_async(scheduler.run())
        self.assertIs(caught.exception.__cause__, failure)
        motor.stop.assert_awaited_once_with()
        self.assertEqual(motor.process.await_args_list, [
            call({"position": 1}), call({"position": 2}),
        ])
        self.assertIn("motor", "\n".join(logs.output))

    def test_cleanup_timeout_rejects_unbounded_or_nonpositive_values(self):
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(cleanup_timeout=timeout), self.assertRaises(ValueError):
                Scheduler([], cleanup_timeout=timeout)

    def test_step_timeout_requests_stop_before_cancelling_original_operation(self):
        active, later = Device("active"), Device("later")

        async def scenario():
            stopped = asyncio.Event()
            cancelled = asyncio.Event()

            async def process(pose):
                try:
                    await stopped.wait()
                    raise RuntimeError("stop acknowledged")
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            async def stop():
                self.assertFalse(cancelled.is_set())
                stopped.set()

            active.process.side_effect = process
            active.stop.side_effect = stop
            scheduler = Scheduler([
                MoveStep(active, {}, 0), MoveStep(later, {}, 1),
            ], step_timeout=0.001, cleanup_timeout=0.1)
            with self.assertLogs("seq_quince.scheduler", level="ERROR"), self.assertRaises(MoveError) as caught:
                await asyncio.wait_for(scheduler.run(), 1)
            self.assertIsInstance(caught.exception.__cause__, asyncio.TimeoutError)
            active.stop.assert_awaited_once_with()
            self.assertFalse(cancelled.is_set())
            later.process.assert_not_awaited()

        self.run_async(scenario())

    def test_cancellation_during_failure_cleanup_propagates_after_settlement(self):
        active, broken, later = [Device(name) for name in ("active", "broken", "later")]

        async def scenario():
            started = asyncio.Event()
            stopping = asyncio.Event()
            allow_stop = asyncio.Event()
            stopped = asyncio.Event()
            settled = asyncio.Event()

            async def process(pose):
                started.set()
                try:
                    await stopped.wait()
                    raise RuntimeError("stopped")
                finally:
                    settled.set()

            async def stop():
                stopping.set()
                await allow_stop.wait()
                stopped.set()

            async def fail(pose):
                await started.wait()
                raise RuntimeError("first failure")

            active.process.side_effect = process
            active.stop.side_effect = stop
            broken.process.side_effect = fail
            scheduler = Scheduler([
                MoveStep(active, {}, 0), MoveStep(broken, {}, 0),
                MoveStep(later, {}, 1),
            ], cleanup_timeout=0.1)
            running = asyncio.create_task(scheduler.run())
            try:
                with self.assertLogs("seq_quince.scheduler", level="ERROR"):
                    await asyncio.wait_for(stopping.wait(), 1)
                    running.cancel()
                    allow_stop.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(running, 1)
                self.assertTrue(settled.is_set())
                active.stop.assert_awaited_once_with()
                later.process.assert_not_awaited()
            finally:
                allow_stop.set()
                stopped.set()

        self.run_async(scenario())

    def test_hanging_rollback_is_bounded_and_remaining_rollback_still_runs(self):
        first, hanging, broken = [Device(name) for name in ("first", "hanging", "broken")]
        failure = RuntimeError("original failure")

        async def scenario():
            rollback_settled = asyncio.Event()

            async def rollback_hangs(pose):
                if pose == {"position": 0}:
                    try:
                        await asyncio.Event().wait()
                    finally:
                        rollback_settled.set()

            async def process_first(pose):
                if pose == {"position": 0}:
                    self.assertTrue(rollback_settled.is_set())

            first.process.side_effect = process_first
            hanging.process.side_effect = rollback_hangs
            broken.process.side_effect = failure
            scheduler = Scheduler([
                MoveStep(first, {"position": 1}, 0),
                MoveStep(hanging, {"position": 2}, 1),
                MoveStep(broken, {}, 2),
            ], cleanup_timeout=0.01)
            with self.assertLogs("seq_quince.scheduler", level="ERROR") as logs, self.assertRaises(MoveError) as caught:
                await asyncio.wait_for(scheduler.run(), 1)
            self.assertIs(caught.exception.__cause__, failure)
            self.assertEqual(first.process.await_args_list, [
                call({"position": 1}), call({"position": 0}),
            ])
            hanging.stop.assert_awaited_once_with()
            self.assertIn("hanging", scheduler.unconfirmed_devices)
            self.assertIn("Rollback timed out for hanging", "\n".join(logs.output))

        self.run_async(scenario())

    def test_cancelled_rollback_operation_does_not_skip_remaining_rollback(self):
        first, cancelled, broken = [Device(name) for name in ("first", "cancelled", "broken")]
        failure = RuntimeError("original failure")

        async def process(pose):
            if pose == {"position": 0}:
                raise asyncio.CancelledError

        cancelled.process.side_effect = process
        broken.process.side_effect = failure
        scheduler = Scheduler([
            MoveStep(first, {"position": 1}, 0),
            MoveStep(cancelled, {"position": 2}, 1),
            MoveStep(broken, {}, 2),
        ], cleanup_timeout=0.1)
        with self.assertLogs("seq_quince.scheduler", level="ERROR") as logs, self.assertRaises(MoveError) as caught:
            self.run_async(scheduler.run())
        self.assertIs(caught.exception.__cause__, failure)
        cancelled.stop.assert_awaited_once_with()
        self.assertEqual(first.process.await_args_list, [
            call({"position": 1}), call({"position": 0}),
        ])
        self.assertIn("Rollback failed for cancelled", "\n".join(logs.output))

    def test_busy_state_failure_preserves_cause_and_blocks_same_device_rollback(self):
        class BusyUnavailableDevice(Device):
            @property
            def busy(self):
                raise OSError("busy readback disconnected")

            @busy.setter
            def busy(self, value):
                pass

        motor = BusyUnavailableDevice("motor")
        failure = RuntimeError("drive failed")

        async def process(pose):
            if pose == {"position": 2}:
                raise failure

        motor.process.side_effect = process
        scheduler = Scheduler([
            MoveStep(motor, {"position": 1}, 0),
            MoveStep(motor, {"position": 2}, 1),
        ], cleanup_timeout=0.1)
        with self.assertLogs("seq_quince.scheduler", level="ERROR") as logs, self.assertRaises(MoveError) as caught:
            self.run_async(scheduler.run())
        self.assertIs(caught.exception.__cause__, failure)
        motor.stop.assert_awaited_once_with()
        self.assertEqual(motor.process.await_args_list, [
            call({"position": 1}), call({"position": 2}),
        ])
        self.assertEqual(scheduler.unconfirmed_devices, ("motor",))
        self.assertIn("busy readback disconnected", "\n".join(logs.output))

    def test_failed_operation_with_idle_busy_state_blocks_prior_device_rollback(self):
        motor = Device("motor")
        failure = RuntimeError("driver failed; queued internal motion may remain")

        async def process(pose):
            if pose == {"position": 2}:
                # A failed gather can leave queued executor children before busy
                # becomes True. Successful stop and idle readback prove too little.
                raise failure

        motor.process.side_effect = process
        scheduler = Scheduler([
            MoveStep(motor, {"position": 1}, 0),
            MoveStep(motor, {"position": 2}, 1),
        ], cleanup_timeout=0.1)
        with self.assertLogs("seq_quince.scheduler", level="ERROR"), self.assertRaises(MoveError) as caught:
            self.run_async(scheduler.run())
        self.assertIs(caught.exception.__cause__, failure)
        self.assertFalse(motor.busy)
        motor.stop.assert_awaited_once_with()
        self.assertEqual(motor.process.await_args_list, [
            call({"position": 1}), call({"position": 2}),
        ])
        self.assertEqual(scheduler.unconfirmed_devices, ("motor",))

    def test_failing_group_rollback_preserves_reverse_configured_step_order(self):
        first, second, broken = Device("first", 10), Device("second", 20), Device("broken")
        failure = RuntimeError("group failed")
        events = []

        async def scenario():
            second_finished = asyncio.Event()
            first_finished = asyncio.Event()

            async def process_first(pose):
                if pose == {"position": 1}:
                    await second_finished.wait()
                    events.append("forward first")
                    first_finished.set()
                else:
                    events.append("rollback first")

            async def process_second(pose):
                if pose == {"position": 2}:
                    events.append("forward second")
                    second_finished.set()
                else:
                    events.append("rollback second")

            async def fail(pose):
                await first_finished.wait()
                raise failure

            first.process.side_effect = process_first
            second.process.side_effect = process_second
            broken.process.side_effect = fail
            scheduler = Scheduler([
                MoveStep(first, {"position": 1}, 0),
                MoveStep(second, {"position": 2}, 0),
                MoveStep(broken, {}, 0),
            ], cleanup_timeout=0.1)
            with self.assertLogs("seq_quince.scheduler", level="ERROR"), self.assertRaises(MoveError) as caught:
                await scheduler.run()
            self.assertIs(caught.exception.__cause__, failure)
            self.assertEqual(events, [
                "forward second", "forward first", "rollback second", "rollback first",
            ])

        self.run_async(scenario())
