import asyncio
from unittest.mock import patch

from seq_quince.device_concrete import sequence

from ._support import DeviceTestCase


class SequenceDeviceTests(DeviceTestCase):
    def setUp(self):
        super().setUp()
        self.init_pvs()

    def test_order_and_pose_desired_config_precedence(self):
        device = sequence.SequenceDevice.from_config_dict(
            {
                "name": "sequence",
                "sequence": {
                    "override": {
                        "pv": "TEST:A",
                        "desired_pv": "TEST:IGNORED",
                        "value": 99,
                        "queue_delay": 0.2,
                    },
                    "desired": {
                        "pv": "TEST:B",
                        "desired_pv": "TEST:DESIRED",
                        "value": 99,
                    },
                    "configured": {"pv": "TEST:C", "value": 3},
                    "empty": {"pv": "TEST:EMPTY"},
                    "missing": {},
                },
            }
        )
        self.pv_factory("TEST:DESIRED").get.return_value = 2
        calls = []
        for name in ("A", "B", "C"):
            self.pvs["TEST:" + name].put.side_effect = lambda value, n=name: (
                calls.append((n, value)) or True
            )

        async def sleep(delay):
            self.assertTrue(device.busy)
            calls.append(("delay", delay))

        self.patch(sequence.asyncio, "sleep", sleep)
        with self.assertLogs(level="ERROR") as logs:
            self.run_async(device.process({"override": 0}))
        self.assertIn("PV for 'missing' not found", "\n".join(logs.output))
        self.assertEqual(
            calls,
            [
                ("A", 0),
                ("delay", 0.2),
                ("B", 2),
                ("delay", 0.01),
                ("C", 3),
                ("delay", 0.01),
            ],
        )
        self.assertNotIn("TEST:IGNORED", self.pvs)
        self.pvs["TEST:EMPTY"].put.assert_not_called()
        self.pv_factory.assert_any_call("TEST:A", queueing_delay=0.2)
        self.pv_factory.assert_any_call("TEST:B", queueing_delay=0.01)
        self.assertFalse(device.busy)
        for name, value in (("A", 10), ("B", 20), ("C", 30), ("EMPTY", 40)):
            self.pvs["TEST:" + name].get.return_value = value
        self.assertEqual(
            self.run_async(device.snapshot()),
            {"override": 10, "desired": 20, "configured": 30, "empty": 40},
        )

    def test_initialization_failure_skips_only_unavailable_channel(self):
        factory = self.pv_factory

        def create(name, **kwargs):
            if name == "TEST:OFFLINE":
                raise ValueError("cannot connect")
            return factory(name, **kwargs)

        self.patch(sequence, "EpicsPV", create)
        with self.assertLogs(level="ERROR") as logs:
            device = sequence.SequenceDevice(
                "sequence",
                {
                    "bad": {"pv": "TEST:OFFLINE", "value": 1},
                    "good": {"pv": "TEST:OK", "value": 2, "queue_delay": 0},
                },
            )
            self.run_async(device.process({}))
        self.assertIn("cannot connect", "\n".join(logs.output))
        self.assertIn("PV for 'bad' not found", "\n".join(logs.output))
        self.pvs["TEST:OK"].put.assert_called_once_with(2)
        self.assertFalse(device.busy)

    def test_put_failure_is_logged_and_busy_cleared(self):
        for outcome, message in [
            (False, "Failed to set"),
            (RuntimeError("offline"), "offline"),
        ]:
            with self.subTest(outcome=outcome):
                self.pvs.clear()
                device = sequence.SequenceDevice(
                    "sequence", {"a": {"pv": "TEST:A", "value": 1, "queue_delay": 0}}
                )
                put = self.pvs["TEST:A"].put
                if isinstance(outcome, Exception):
                    put.side_effect = outcome
                else:
                    put.return_value = outcome
                with self.assertLogs(level="ERROR") as logs:
                    self.run_async(device.process({}))
                self.assertIn(message, "\n".join(logs.output))
                self.assertFalse(device.busy)

    def test_stop_aborts_before_next_write_and_duplicate_process_is_ignored(self):
        device = sequence.SequenceDevice(
            "sequence",
            {
                "a": {"pv": "TEST:A", "value": 1},
                "b": {"pv": "TEST:B", "value": 2},
            },
        )
        real_sleep = asyncio.sleep

        async def scenario():
            entered = asyncio.Event()
            release = asyncio.Event()

            async def sleep(delay):
                if delay == 0.01:
                    entered.set()
                    await release.wait()
                else:
                    release.set()
                    await real_sleep(0)

            with patch.object(sequence.asyncio, "sleep", sleep):
                task = asyncio.create_task(device.process({}))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    self.assertTrue(device.busy)
                    await device.process({"a": 99})
                    await device.stop()
                    await asyncio.wait_for(task, 1)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        with self.assertLogs(level="WARNING") as logs:
            self.run_async(scenario())
        self.assertIn("already running", "\n".join(logs.output))
        self.pvs["TEST:A"].put.assert_called_once_with(1)
        self.pvs["TEST:B"].put.assert_not_called()
        self.assertFalse(device.busy)
