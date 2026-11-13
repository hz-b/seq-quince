import asyncio
import logging
import threading
from unittest.mock import patch

from seq_quince.device_concrete import wait_condition as wait

from ._support import DeviceTestCase


class WaitConditionDeviceTests(DeviceTestCase):
    def setUp(self):
        super().setUp()
        self.init_pvs()

    @staticmethod
    def make_device(**overrides):
        return wait.WaitConditionDevice.from_config_dict(
            dict(
                {"name": "wait", "pv": "TEST:STATE", "value": 1, "poll_period": 0.001},
                **overrides,
            )
        )

    def test_config_and_pose_precedence_preserve_defaults(self):
        device = self.make_device(
            options={"value": 9, "poll_period": 0.1, "log_every": 2}, value=0
        )
        self.assertEqual(
            (device.default_value, device.poll_period, device.log_every), (0, 0.001, 2)
        )
        self.assertFalse(device.busy)
        self.assertFalse(device.supports_rollback())
        self.assertIsNone(device.scheduler_timeout({"timeout": 1}))
        self.pv_factory.assert_not_called()
        options = device._resolve_options(
            {
                "options": {
                    "pv": "TEST:OTHER",
                    "value": 2,
                    "timeout": 3,
                    "condition": "gt",
                    "frequency": 5,
                },
                "value": 4,
                "watchdog_pv": "TEST:WD",
                "frequency": 10,
            }
        )
        self.assertEqual(
            (options.pv, options.value, options.condition, options.timeout),
            ("TEST:OTHER", 4, "gt", 3),
        )
        self.assertTrue(options.watchdog_enabled)
        self.assertEqual(options.watchdog_period, 0.1)
        self.assertEqual(device.default_pv, "TEST:STATE")
        self.assertEqual(device.default_value, 0)
        disabled = device._resolve_options({"watchdog_pv": "", "frequency": 10})
        self.assertFalse(disabled.watchdog_enabled)
        self.assertIsNone(disabled.watchdog_frequency)
        self.assertIsNone(disabled.watchdog_period)

    def test_nested_config_accepts_frequency_alias(self):
        device = wait.WaitConditionDevice.from_config_dict(
            {
                "name": "wait",
                "options": {
                    "pv": "TEST:STATE",
                    "value": "ready",
                    "condition": " EQUALS ",
                    "poll_period": "0.1",
                    "timeout": "2",
                    "pv_read_timeout": "0.3",
                    "read_retry_count": "2",
                    "max_consecutive_read_failures": "4",
                    "log_every": "3",
                    "watchdog_pv": "TEST:WD",
                    "frequency": "5",
                },
            }
        )
        options = device._resolve_options(None)
        self.assertEqual(
            (options.pv, options.value, options.condition),
            ("TEST:STATE", "ready", "eq"),
        )
        self.assertEqual(
            (options.poll_period, options.timeout, options.pv_read_timeout),
            (0.1, 2, 0.3),
        )
        self.assertEqual(
            (
                options.read_retry_count,
                options.max_consecutive_read_failures,
                options.log_every,
            ),
            (2, 4, 3),
        )
        self.assertEqual(options.watchdog_period, 0.2)

    def test_invalid_runtime_options_never_open_channels_or_set_busy(self):
        cases = [
            ({"pv": None}, "'pv' is required"),
            ({"value": None}, "'value' is required"),
            ({"condition": "bogus"}, "Invalid condition"),
            ({"poll_period": 0}, "poll_period"),
            ({"timeout": -1}, "timeout"),
            ({"pv_read_timeout": 0}, "pv_read_timeout"),
            ({"read_retry_count": 0}, "read_retry_count"),
            ({"max_consecutive_read_failures": 0}, "max_consecutive_read_failures"),
            ({"log_every": 0}, "log_every"),
            ({"watchdog_pv": "TEST:WD", "frequency": 0}, "watchdog_frequency"),
        ]
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                device = self.make_device()
                with self.assertRaisesRegex(ValueError, message):
                    self.run_async(device.process(overrides))
                self.assertFalse(device.busy)
                self.pv_factory.assert_not_called()

    def test_invalid_constructor_options(self):
        cases = [
            ({"poll_period": 0}, "poll_period"),
            ({"timeout": -1}, "timeout"),
            ({"pv_read_timeout": 0}, "pv_read_timeout"),
            ({"watchdog_frequency": 0}, "watchdog_frequency"),
            ({"watchdog_frequency": 1}, "watchdog_pv is missing"),
        ]
        for options, message in cases:
            with self.subTest(options=options), self.assertRaisesRegex(
                ValueError, message
            ):
                self.make_device(**options)

    def test_comparisons_cover_numeric_boundaries_and_text(self):
        cases = [
            (" 2 ", 2, "==", True),
            (2, "2", "ne", False),
            (2, 1, ">", True),
            (2, 2, "gt", False),
            (2, 2, ">=", True),
            (1, 2, "ge", False),
            (1, 2, "<", True),
            (2, 2, "lt", False),
            (2, 2, "<=", True),
            (3, 2, "le", False),
            (" READY ", "READY", " EQUALS ", True),
            ("RUN", "READY", "not_equal", True),
            (None, None, "eq", True),
            (False, 0, "eq", True),
        ]
        for current, expected, condition, result in cases:
            with self.subTest(current=current, expected=expected, condition=condition):
                self.assertIs(
                    wait.WaitConditionDevice._evaluate_condition(
                        current, expected, condition
                    ),
                    result,
                )
        with self.assertRaises(ValueError):
            wait.WaitConditionDevice._evaluate_condition("READY", 1, "gt")

    def test_process_polls_until_satisfied_and_reuses_channel(self):
        for level in (logging.INFO, logging.DEBUG):
            with self.subTest(log_level=level):
                self.pvs.clear()
                device = self.make_device(pv_read_timeout=0.125, log_every=2)
                pv = self.pv_factory("TEST:STATE")
                pv.get.side_effect = [0, 0, 1]
                self.pv_factory.reset_mock()
                with self.assertLogs(wait.__name__, level=level) as logs:
                    self.run_async(device.process({}))
                self.assertEqual(pv.get.call_count, 3)
                self.assertTrue(
                    all(
                        item.kwargs == {"timeout": 0.125}
                        for item in pv.get.call_args_list
                    )
                )
                self.pv_factory.assert_called_once_with("TEST:STATE")
                pv.put.assert_not_called()
                self.assertIn("condition met after 3 samples", "\n".join(logs.output))
                self.assertFalse(device.busy)

    def test_read_retry_recovers_and_consecutive_failure_counter_resets(self):
        device = self.make_device(read_retry_count=1, max_consecutive_read_failures=2)
        pv = self.pv_factory("TEST:STATE")
        pv.get.side_effect = [ValueError("first"), 0, ValueError("second"), 1]
        self.run_async(device.process({}))
        self.assertEqual(pv.get.call_count, 4)
        self.assertFalse(device.busy)
        pv.get.reset_mock(side_effect=True)
        pv.get.side_effect = [ValueError("transient"), 1]
        self.run_async(device.process({"read_retry_count": 2}))
        self.assertEqual(pv.get.call_count, 2)

    def test_persistent_read_error_preserves_cause_and_clears_busy(self):
        device = self.make_device(read_retry_count=2, max_consecutive_read_failures=2)
        pv = self.pv_factory("TEST:STATE")
        error = OSError("offline")
        pv.get.side_effect = error
        with self.assertRaisesRegex(
            RuntimeError, "maximum consecutive PV read failures"
        ) as caught:
            self.run_async(device.process({}))
        self.assertEqual(pv.get.call_count, 4)
        self.assertIs(caught.exception.__cause__.__cause__, error)
        self.assertFalse(device.busy)

    def test_timeout_is_reported_and_clears_busy(self):
        device = self.make_device(timeout=1e-12)
        self.pv_factory("TEST:STATE").get.return_value = 0
        with self.assertRaisesRegex(TimeoutError, "condition not met"):
            self.run_async(device.process({}))
        self.assertFalse(device.busy)

    def _check_stop_or_cancellation(self, cancel):
        device = self.make_device()
        pv = self.pv_factory("TEST:STATE")
        real_sleep = asyncio.sleep

        async def scenario():
            entered = asyncio.Event()
            poll_gate = asyncio.Event()

            async def sleep(_delay):
                if _delay == device.poll_period:
                    # Hold the polling wait so cancellation cannot race with
                    # dispatch of the next executor read.
                    entered.set()
                    await poll_gate.wait()
                else:
                    await real_sleep(0)

            with patch.object(wait.asyncio, "sleep", sleep):
                task = asyncio.create_task(device.process({}))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    self.assertTrue(device.busy)
                    with self.assertRaisesRegex(RuntimeError, "already active"):
                        await device.process({})
                    if cancel:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        # stop() sets the abort flag before its first yield.
                        poll_gate.set()
                        await device.stop()
                        with self.assertRaisesRegex(RuntimeError, "aborted"):
                            await asyncio.wait_for(task, 1)
                finally:
                    poll_gate.set()
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self.assertFalse(device.busy)
                self.assertEqual(pv.get.call_count, 1)
                pv.get.return_value = 1
                await device.process({})

        self.run_async(scenario())
        self.assertFalse(device.busy)

    def test_stop_cleans_up_and_allows_reuse(self):
        self._check_stop_or_cancellation(False)

    def test_cancellation_cleans_up_and_allows_reuse(self):
        self._check_stop_or_cancellation(True)

    def test_cancelled_read_blocks_reuse_and_cleanup_until_worker_finishes(self):
        device = self.make_device()
        pv = self.pv_factory("TEST:STATE")
        release = threading.Event()

        async def scenario():
            entered = asyncio.Event()
            loop = asyncio.get_running_loop()

            def read(**_kwargs):
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(2):
                    raise TimeoutError("Test did not release the read worker")
                return 0

            pv.get.side_effect = read
            task = asyncio.create_task(device.process({}))
            closing = None
            try:
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(device.busy)
                with self.assertRaisesRegex(RuntimeError, "already active"):
                    await device.process({})
                self.assertEqual(pv.get.call_count, 1)

                closing = asyncio.create_task(device.close())
                await asyncio.sleep(0)
                self.assertFalse(closing.done())
                pv.close.assert_not_called()
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                if closing is not None:
                    await asyncio.wait_for(closing, 1)
            self.assertFalse(device.busy)
            pv.close.assert_called_once_with()

        self.run_async(scenario())

    def test_watchdog_resets_periodically_and_is_cancelled_on_completion(self):
        for put_ok in (True, False):
            with self.subTest(put_ok=put_ok):
                self.pvs.clear()
                device = self.make_device(
                    watchdog_pv="TEST:WD", watchdog_frequency=1000
                )
                watchdog = self.pv_factory("TEST:WD")
                original_read = device._read_value_async

                async def scenario(
                    device=device,
                    watchdog=watchdog,
                    original_read=original_read,
                    put_ok=put_ok,
                ):
                    enough_resets = asyncio.Event()
                    loop = asyncio.get_running_loop()

                    def record_reset(value):
                        self.assertEqual(value, 0)
                        if watchdog.put.call_count >= 2:
                            loop.call_soon_threadsafe(enough_resets.set)
                        return put_ok

                    watchdog.put.side_effect = record_reset

                    async def read(pv, timeout):
                        await asyncio.wait_for(enough_resets.wait(), 1)
                        return await original_read(pv, timeout)

                    with patch.object(device, "_read_value_async", read):
                        self.pv_factory("TEST:STATE").get.return_value = 1
                        await asyncio.wait_for(device.process({}), 2)
                    count = watchdog.put.call_count
                    self.assertGreaterEqual(count, 2)
                    await asyncio.sleep(0)
                    self.assertEqual(watchdog.put.call_count, count)
                    self.assertFalse(
                        any(
                            task.get_name() == "wait-watchdog"
                            for task in asyncio.all_tasks()
                        )
                    )

                with self.assertLogs(wait.__name__, level="INFO") as logs:
                    self.run_async(scenario())
                self.assertIs(
                    "failed to reset watchdog" in "\n".join(logs.output), not put_ok
                )
                self.assertFalse(device.busy)

    def test_snapshot_reads_live_value_and_tolerates_disconnection(self):
        device = self.make_device()
        pv = self.pv_factory("TEST:STATE")
        pv.get.return_value = "ready"
        expected = {
            "pv": "TEST:STATE",
            "current_value": "ready",
            "value": 1,
            "condition": "eq",
            "poll_period": 0.001,
            "timeout": 0,
            "watchdog_pv": None,
            "watchdog_frequency": None,
        }
        self.assertEqual(self.run_async(device.snapshot()), expected)
        pv.get.side_effect = OSError("offline")
        self.assertEqual(
            self.run_async(device.snapshot()), dict(expected, current_value=None)
        )
        unconfigured = wait.WaitConditionDevice("runtime-only")
        self.assertIsNone(self.run_async(unconfigured.snapshot())["current_value"])
