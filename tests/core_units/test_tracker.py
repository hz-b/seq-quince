"""Commands, pending work and settlement of the per-device work tracker."""

import asyncio
import gc
import threading
import time
from unittest.mock import patch

from seq_quince.device._tracker import Tracker

from ._support import CoreTestCase


class TrackerTests(CoreTestCase):
    def test_only_commands_are_pending_work_and_advance_the_generation(self):
        async def scenario():
            loop = asyncio.get_running_loop()
            tracker = Tracker()
            other = loop.create_future()  # A readiness read or a scheduler step.
            command = loop.create_future()
            tracker.track(other)
            self.assertFalse(tracker.pending())
            self.assertEqual(tracker.generation, 0)

            tracker.track(command, command=True)
            self.assertTrue(tracker.pending())
            self.assertEqual(tracker.generation, 1)
            self.assertEqual(tracker.commands(), (command,))

            # Finished work stops counting at once; the generation never rewinds.
            command.set_result(None)
            self.assertFalse(tracker.pending())
            self.assertEqual(tracker.generation, 1)

            # Disposal waits for the other work as well.
            settling = asyncio.create_task(tracker.wait_all())
            done, _ = await asyncio.wait({settling}, timeout=0.05)
            self.assertFalse(done)
            other.set_result(None)
            await asyncio.wait_for(settling, 1)

        self.run_async(scenario())

    def test_failed_and_cancelled_work_is_untracked_and_its_failure_observed(self):
        unobserved = []
        self.loop.set_exception_handler(lambda loop, context: unobserved.append(context))

        async def scenario():
            loop = asyncio.get_running_loop()
            tracker = Tracker()
            failed = loop.create_future()
            cancelled = loop.create_future()
            tracker.track(failed, command=True)
            tracker.track(cancelled)
            failed.set_exception(RuntimeError("worker failed after its waiter left"))
            cancelled.cancel()
            await asyncio.wait_for(tracker.wait_all(), 1)
            self.assertFalse(tracker.pending())

        self.run_async(scenario())
        gc.collect()  # An unobserved failure is reported when its future is freed.
        self.run_async(asyncio.sleep(0))
        self.assertEqual(unobserved, [])

    def test_wait_all_includes_work_started_meanwhile_and_skips_the_caller(self):
        async def scenario():
            loop = asyncio.get_running_loop()
            tracker = Tracker()
            first = loop.create_future()
            second = loop.create_future()
            tracker.track(first, command=True)
            # Like a sequence that dispatches its next move when one completes.
            first.add_done_callback(lambda done: tracker.track(second, command=True))

            async def settle():
                current = asyncio.current_task()
                tracker.track(current)
                await tracker.wait_all(exclude=current)

            settling = asyncio.create_task(settle())
            first.set_result(None)
            done, _ = await asyncio.wait({settling}, timeout=0.05)
            self.assertFalse(done)
            self.assertEqual(tracker.commands(), (second,))
            second.set_result(None)
            await asyncio.wait_for(settling, 1)

        self.run_async(scenario())

    def test_pending_work_can_be_read_from_a_worker_thread(self):
        async def scenario():
            loop = asyncio.get_running_loop()
            tracker = Tracker()
            command = loop.create_future()
            tracker.track(command, command=True)
            self.assertTrue(await loop.run_in_executor(None, tracker.pending))
            command.set_result(None)
            self.assertFalse(await loop.run_in_executor(None, tracker.pending))

        self.run_async(scenario())

    def test_dispatched_work_is_pending_before_its_worker_can_look(self):
        async def scenario():
            loop = asyncio.get_running_loop()
            tracker = Tracker()
            running = threading.Event()
            submit = loop.run_in_executor

            def submit_slowly(executor, function, *args):
                # The worker runs well before the dispatching call returns.
                future = submit(executor, function, *args)
                running.wait(1)
                time.sleep(0.02)
                return future

            def work():
                running.set()
                return tracker.pending()

            with patch.object(loop, "run_in_executor", submit_slowly):
                command = tracker.dispatch(work, command=True)
            self.assertTrue(await asyncio.wait_for(command, 1))
            self.assertFalse(tracker.pending())

        self.run_async(scenario())
