"""Exercise scheduling and rollback through real coroutine coordination."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

from seq_quince.scheduler import MoveError, Scheduler
from seq_quince.sequence import MoveStep, _BarrierDevice

from ._support import CoreTestCase


class Device:
    """A device boundary with inspectable async operations; no hardware."""

    def __init__(self, name, initial=0):
        self.name = name
        self.snapshot = AsyncMock(return_value={"position": initial})
        self.process = AsyncMock()
        self.stop = AsyncMock()
        self.busy = False
        self.scheduler_timeout = Mock(return_value=None)
        self.supports_rollback = Mock(return_value=True)


class SchedulerTests(CoreTestCase):
    def test_group_is_parallel_and_next_group_waits_for_completion(self):
        first, second, last = [Device(name) for name in ("first", "second", "last")]

        async def scenario():
            started = set()
            both_started = asyncio.Event()
            released = asyncio.Event()
            finished = set()

            async def move(name):
                started.add(name)
                if len(started) == 2:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), 1)
                released.set()
                finished.add(name)

            async def final_move(pose):
                self.assertTrue(released.is_set())
                self.assertEqual(finished, {"first", "second"})
                self.assertEqual(pose, {"position": 3})

            async def first_move(pose):
                self.assertEqual(pose, {"position": 1})
                await move("first")

            async def second_move(pose):
                self.assertEqual(pose, {"position": 2})
                await move("second")

            first.process.side_effect = first_move
            second.process.side_effect = second_move
            last.process.side_effect = final_move
            steps = [
                MoveStep(first, {"position": 1}, 0),
                MoveStep(second, {"position": 2}, 0),
                MoveStep(last, {"position": 3}, 1),
                MoveStep(_BarrierDevice(), {}, None),
            ]
            scheduler = Scheduler(steps)
            steps.clear()  # Constructor owns its step list.
            await asyncio.wait_for(scheduler.run(), 2)
            self.assertEqual(len(scheduler._finished), 3)

        self.run_async(scenario())
        for device in (first, second, last):
            device.snapshot.assert_awaited_once_with()
            self.assertEqual(device.process.await_count, 1)
            device.stop.assert_not_awaited()

    def test_every_group_snapshot_precedes_any_group_motion(self):
        first, second = Device("first"), Device("second")
        captured = set()

        async def capture(name):
            captured.add(name)
            return {"position": 0}

        async def first_snapshot():
            return await capture("first")

        async def second_snapshot():
            return await capture("second")

        async def process(pose):
            self.assertEqual(captured, {"first", "second"})

        first.snapshot.side_effect = first_snapshot
        second.snapshot.side_effect = second_snapshot
        first.process.side_effect = second.process.side_effect = process
        scheduler = Scheduler([MoveStep(first, {}, 0), MoveStep(second, {}, 0)])
        self.run_async(scheduler.run())

    def test_timeout_precedence_and_disabled_values(self):
        cases = [
            (None, None, None),
            (None, 3, 3.0),
            (2, 3, 2.0),
            (0, 3, None),
            (-1, 3, None),
            (None, 0, None),
            ("0.5", 3, 0.5),
        ]
        for device_timeout, fallback, expected in cases:
            with self.subTest(device_timeout=device_timeout, fallback=fallback):
                device = Device("motor")
                device.scheduler_timeout.return_value = device_timeout
                pose = {"position": 1}
                step = MoveStep(device, pose, 0)
                self.assertEqual(
                    Scheduler([step], step_timeout=fallback)._effective_timeout(step),
                    expected,
                )
                device.scheduler_timeout.assert_called_once_with(pose)

    def test_real_timeout_settles_pending_coroutine_and_preserves_cause(self):
        device = Device("motor")

        async def scenario():
            cancelled = asyncio.Event()

            async def never_finishes(pose):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            device.process.side_effect = never_finishes
            scheduler = Scheduler(
                [MoveStep(device, {"position": 1}, 0)],
                step_timeout=0.001,
                cleanup_timeout=0.01,
            )
            with self.assertLogs("seq_quince.scheduler", level="ERROR"), self.assertRaises(
                MoveError
            ) as caught:
                await asyncio.wait_for(scheduler.run(), 1)
            self.assertIsInstance(caught.exception.__cause__, asyncio.TimeoutError)
            self.assertTrue(cancelled.is_set())
            device.stop.assert_awaited_once_with()
            self.assertEqual(scheduler.unconfirmed_devices, ("motor",))
            self.assertEqual(device.process.await_count, 1)

        self.run_async(scenario())

    def test_failure_rolls_completed_steps_back_in_reverse_order(self):
        events = []
        first, second, broken = (
            Device("first", 10),
            Device("second", 20),
            Device("broken", 30),
        )
        error = RuntimeError("drive failure")

        async def first_move(pose):
            events.append(("first", pose.copy()))

        async def second_move(pose):
            events.append(("second", pose.copy()))

        first.process.side_effect = first_move
        second.process.side_effect = second_move
        broken.process.side_effect = error
        scheduler = Scheduler(
            [
                MoveStep(first, {"position": 1}, 0),
                MoveStep(second, {"position": 2}, 1),
                MoveStep(broken, {"position": 3}, 2),
            ]
        )
        with self.assertLogs("seq_quince.scheduler", level="ERROR"), self.assertRaises(
            MoveError
        ) as caught:
            self.run_async(scheduler.run())
        self.assertIs(caught.exception.__cause__, error)
        self.assertEqual(
            events,
            [
                ("first", {"position": 1}),
                ("second", {"position": 2}),
                ("second", {"position": 20}),
                ("first", {"position": 10}),
            ],
        )
        broken.process.assert_awaited_once_with({"position": 3})

    def test_rollback_failure_is_logged_and_remaining_rollback_still_runs(self):
        first, second, broken = (
            Device("first", 10),
            Device("second", 20),
            Device("broken"),
        )
        original_error = RuntimeError("original failure")
        second.process.side_effect = [None, OSError("rollback failure")]
        broken.process.side_effect = original_error
        scheduler = Scheduler(
            [
                MoveStep(first, {"position": 1}, 0),
                MoveStep(second, {"position": 2}, 1),
                MoveStep(broken, {}, 2),
            ]
        )
        with self.assertLogs(
            "seq_quince.scheduler", level="ERROR"
        ) as logs, self.assertRaises(MoveError) as caught:
            self.run_async(scheduler.run())
        self.assertIs(caught.exception.__cause__, original_error)
        self.assertIn("Rollback failed for second", "\n".join(logs.output))
        self.assertEqual(
            first.process.await_args_list,
            [call({"position": 1}), call({"position": 10})],
        )

    def test_nonrollback_device_is_neither_snapshotted_nor_restored(self):
        wait, broken = Device("wait"), Device("broken")
        wait.supports_rollback.return_value = False
        broken.process.side_effect = RuntimeError("failed")
        scheduler = Scheduler(
            [MoveStep(wait, {"value": 1}, 0), MoveStep(broken, {}, 1)]
        )
        with self.assertLogs(
            "seq_quince.scheduler", level="INFO"
        ) as logs, self.assertRaises(MoveError):
            self.run_async(scheduler.run())
        wait.snapshot.assert_not_awaited()
        wait.process.assert_awaited_once_with({"value": 1})
        self.assertIn("Skipping rollback for wait", "\n".join(logs.output))

    def test_missing_rollback_snapshot_is_logged_without_new_motion(self):
        first, broken = Device("first"), Device("broken")
        first.supports_rollback.side_effect = [False, True]
        broken.process.side_effect = RuntimeError("failed")
        scheduler = Scheduler(
            [MoveStep(first, {"position": 1}, 0), MoveStep(broken, {}, 1)]
        )
        with self.assertLogs(
            "seq_quince.scheduler", level="WARNING"
        ) as logs, self.assertRaises(MoveError):
            self.run_async(scheduler.run())
        first.process.assert_awaited_once_with({"position": 1})
        self.assertIn(
            "No rollback snapshot available for first", "\n".join(logs.output)
        )

    def test_snapshot_failure_prevents_group_motion(self):
        device = Device("broken")
        error = OSError("cannot read position")
        device.snapshot.side_effect = error
        with self.assertLogs("seq_quince.scheduler", level="ERROR"), self.assertRaises(
            MoveError
        ) as caught:
            self.run_async(Scheduler([MoveStep(device, {"position": 1}, 0)]).run())
        self.assertIs(caught.exception.__cause__, error)
        device.process.assert_not_awaited()

    def test_snapshot_deduplicates_devices_and_omits_barrier(self):
        first, second = Device("first", 1), Device("second", 2)
        scheduler = Scheduler(
            [
                MoveStep(first, {}, 0),
                MoveStep(first, {}, 1),
                MoveStep(second, {}, 1),
                MoveStep(_BarrierDevice(), {}, None),
            ]
        )
        self.assertEqual(
            self.run_async(scheduler.snapshot()),
            {
                "first": {"position": 1},
                "second": {"position": 2},
            },
        )
        first.snapshot.assert_awaited_once_with()
        second.snapshot.assert_awaited_once_with()

    def test_empty_sequence_and_legacy_rollback_capability_default(self):
        scheduler = Scheduler([])
        self.assertIsNone(self.run_async(scheduler.run()))
        self.assertEqual(self.run_async(scheduler.snapshot()), {})
        self.assertTrue(scheduler._supports_rollback(SimpleNamespace()))
