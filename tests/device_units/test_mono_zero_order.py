import asyncio
import math
import unittest
from collections import deque
from unittest.mock import AsyncMock, patch

from seq_quince.device_concrete import mono_zero_order as zero_order


class FakePV:
    """Return scripted values/errors, then repeat the last result."""

    def __init__(self, pvname):
        self.pvname = pvname
        self.results = deque([0.0])
        self.get_timeouts = []
        self.put_values = []
        self.close_calls = 0

    def close(self):
        self.close_calls += 1

    def script(self, *results):
        if not results:
            raise ValueError("Provide at least one result")
        self.results = deque(results)

    def get(self, timeout=None):
        self.get_timeouts.append(timeout)
        result = self.results[0]
        if len(self.results) > 1:
            self.results.popleft()
        if isinstance(result, Exception):
            raise result
        return result

    def put(self, value):
        self.put_values.append(value)


class DeviceFixture:
    """Patch only the device module's PV dependency, never real hardware."""

    def setUp(self):
        super().setUp()
        pv_patch = patch.object(zero_order, "EpicsPV", FakePV)
        pv_patch.start()
        self.addCleanup(pv_patch.stop)

    @staticmethod
    def make_device(**overrides):
        config = {
            "name": "zero_order",
            "alpha_pv": "TEST:ALPHA",
            "beta_pv": "TEST:BETA",
            "theta_pv": "TEST:THETA",
        }
        config.update(overrides)
        return zero_order.MonoZeroOrderDevice.from_config_dict(config)


class ConstructorTests(DeviceFixture, unittest.TestCase):
    """Current Python API: deliberately isolated for a future API migration."""

    def test_constructor_without_current_event_loop(self):
        previous_policy = asyncio.get_event_loop_policy()
        asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
        try:
            # Match the state left by asyncio.run() or an async test's cleanup.
            asyncio.set_event_loop(None)
            device = self.make_device(stable_reads=1)
            self.assertFalse(device.busy)
            asyncio.run(device.process({}))
            self.assertFalse(device.busy)
            self.assertEqual(len(device.alpha_pv.get_timeouts), 1)
        finally:
            asyncio.set_event_loop_policy(previous_policy)

    def test_direct_constructor_defaults(self):
        device = zero_order.MonoZeroOrderDevice(
            "direct", "TEST:A", "TEST:B", "TEST:T"
        )
        self.assertEqual(device.name, "direct")
        self.assertEqual(device.alpha_pv.pvname, "TEST:A")
        self.assertEqual(device.beta_pv.pvname, "TEST:B")
        self.assertEqual(device.theta_pv.pvname, "TEST:T")
        self.assertEqual(device.mode, "stddev")
        self.assertEqual(device.stable_reads, 3)

    def test_direct_constructor_normalizes_values_and_copies_reference(self):
        reference = {"alpha": "1", "beta": 2, "theta": 3}
        device = zero_order.MonoZeroOrderDevice(
            "direct", "TEST:A", "TEST:B", "TEST:T",
            mode=" SPREAD ", in_position_band="0.25", poll_period="0.1",
            stable_reads=0, timeout="-1", log_every=0,
            pv_read_timeout="0.2", read_retry_count=0,
            max_consecutive_read_failures=0, reference=reference,
        )
        reference["alpha"] = 99
        self.assertEqual(device.mode, "spread")
        self.assertEqual(device.in_position_band, 0.25)
        self.assertEqual(device.poll_period, 0.1)
        self.assertEqual(device.timeout, -1.0)
        self.assertEqual(device.pv_read_timeout, 0.2)
        for key in ("stable_reads", "log_every", "read_retry_count",
                    "max_consecutive_read_failures"):
            self.assertEqual(getattr(device, key), 1)
        self.assertEqual(device.reference_alpha, 1.0)


class ConfigurationTests(DeviceFixture, unittest.TestCase):
    def test_defaults(self):
        device = self.make_device()
        expected = {
            "mode": "stddev", "in_position_band": 0.005,
            "poll_period": 0.2, "stable_reads": 3, "timeout": 60.0,
            "log_every": 10, "pv_read_timeout": 0.5,
            "read_retry_count": 3, "max_consecutive_read_failures": 5,
            "reference_alpha": None, "reference_beta": None,
            "reference_theta": None,
        }
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertEqual(getattr(device, key), value)
        self.assertIsNone(device.watchdog_pv)
        self.assertIsNone(device.pause_pv)
        self.assertFalse(device.busy)
        self.assertFalse(device.supports_rollback())
        self.assertIsNone(device.scheduler_timeout({"timeout": 1}))

    def test_all_nested_options_and_optional_pvs(self):
        options = {
            "mode": " REFERENCE ", "in_position_band": "0.2",
            "poll_period": "0.01", "stable_reads": "4", "timeout": "12",
            "log_every": "2", "pv_read_timeout": "0.3",
            "read_retry_count": "2", "max_consecutive_read_failures": "6",
            "reference": {"alpha": "1", "beta": 2, "theta": 3},
        }
        device = self.make_device(
            options=options, watchdog_pv="TEST:WD", pause_pv="TEST:PAUSE"
        )
        self.assertEqual(device.mode, "reference")
        for key in ("in_position_band", "poll_period", "stable_reads", "timeout",
                    "log_every", "pv_read_timeout", "read_retry_count",
                    "max_consecutive_read_failures"):
            with self.subTest(key=key):
                self.assertEqual(getattr(device, key), float(options[key]))
        self.assertEqual(device._default_options().reference_dict(),
                         {"alpha": 1.0, "beta": 2.0, "theta": 3.0})
        self.assertEqual(device.watchdog_pv.pvname, "TEST:WD")
        self.assertEqual(device.pause_pv.pvname, "TEST:PAUSE")

    def test_top_level_options_override_nested_options(self):
        top = {
            "mode": "spread", "in_position_band": 0.2, "poll_period": 0.1,
            "stable_reads": 2, "timeout": 4, "log_every": 3,
            "pv_read_timeout": 0.4, "read_retry_count": 4,
            "max_consecutive_read_failures": 6,
            "reference": {"alpha": 1, "beta": 2, "theta": 3},
        }
        nested = dict(top, mode="stddev", in_position_band=99,
                      reference={"alpha": 99})
        for key in top:
            if key not in ("mode", "reference"):
                nested[key] = 99
        device = self.make_device(options=nested, **top)
        for key, value in top.items():
            if key != "reference":
                with self.subTest(key=key):
                    self.assertEqual(getattr(device, key), value)
        self.assertEqual(device._default_options().reference_dict(), top["reference"])

    def test_legacy_band_alias_and_precedence(self):
        cases = [
            ({"deviation_threshold": 0.1}, 0.1),
            ({"options": {"deviation_threshold": 0.2}}, 0.2),
            ({"in_position_band": 0.3, "deviation_threshold": 0.4}, 0.3),
            ({"deviation_threshold": 0.5,
              "options": {"in_position_band": 0.6}}, 0.5),
        ]
        for config, expected in cases:
            with self.subTest(config=config):
                self.assertEqual(self.make_device(**config).in_position_band, expected)

    def test_missing_required_configuration(self):
        for key in ("name", "alpha_pv", "beta_pv", "theta_pv"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                self.make_device(**{key: None})

    def test_invalid_configuration(self):
        for key, value in (("mode", "invalid"), ("in_position_band", -1),
                           ("poll_period", 0), ("pv_read_timeout", 0)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.make_device(**{key: value})

    def test_runtime_overrides_do_not_change_defaults(self):
        device = self.make_device()
        before = device._default_options()
        pose = {
            "options": {"mode": "spread", "in_position_band": 9,
                        "poll_period": 0.01, "stable_reads": 2, "timeout": 7,
                        "log_every": 2, "pv_read_timeout": 0.1,
                        "read_retry_count": 1, "max_consecutive_read_failures": 2},
            "mode": " REFERENCE ", "deviation_threshold": 0.2,
        }
        resolved = device._resolve_options(pose)
        self.assertEqual(resolved.mode, "reference")
        self.assertEqual(resolved.in_position_band, 0.2)
        for key, value in pose["options"].items():
            if key not in ("mode", "in_position_band"):
                self.assertEqual(getattr(resolved, key), value)
        self.assertEqual(device._default_options(), before)

    def test_reference_override_precedence(self):
        device = self.make_device(reference={"alpha": 1, "beta": 2, "theta": 3})
        pose = {"options": {"reference": {"alpha": 10}, "reference_alpha": 30},
                "reference": {"alpha": 20}, "reference_alpha": 40}
        self.assertEqual(device._resolve_options(pose).reference_dict(),
                         {"alpha": 40.0, "beta": 2.0, "theta": 3.0})
        del pose["reference_alpha"]
        self.assertEqual(device._resolve_options(pose).reference_alpha, 30)
        del pose["options"]["reference_alpha"]
        self.assertEqual(device._resolve_options(pose).reference_alpha, 20)
        del pose["reference"]
        self.assertEqual(device._resolve_options(pose).reference_alpha, 10)
        pose.update(alpha=4, beta=5, theta=6, reference_alpha=99)
        self.assertEqual(device._resolve_options(pose).reference_dict(),
                         {"alpha": 4.0, "beta": 5.0, "theta": 6.0})
        self.assertEqual(device.reference_alpha, 1)

    def test_metrics(self):
        device = self.make_device()
        values = {"alpha": 1, "beta": 2, "theta": 3}
        for mode, expected in (("stddev", math.sqrt(2 / 3)), ("spread", 2),
                               ("reference", 1)):
            with self.subTest(mode=mode):
                options = device._resolve_options({
                    "mode": mode, "reference": {"alpha": 0, "beta": 2, "theta": 4}
                })
                self.assertAlmostEqual(device._metric(values, options), expected)
        with self.assertRaisesRegex(ValueError, "reference mode requires"):
            device._metric(values, device._resolve_options({"mode": "reference"}))

    def test_pv_read_retries_and_read_timeout(self):
        device = self.make_device(read_retry_count=2, pv_read_timeout=0.125)
        device.alpha_pv.script(ValueError("transient"), "1.5")
        values = device._read_values(device._default_options())
        self.assertEqual(values["alpha"], 1.5)
        self.assertEqual(device.alpha_pv.get_timeouts, [0.125, 0.125])

    def test_pv_read_failure_preserves_cause(self):
        device = self.make_device(read_retry_count=2)
        error = ValueError("offline")
        device.alpha_pv.script(error)
        with self.assertRaisesRegex(ValueError, "after 2 attempts") as caught:
            device._read_values(device._default_options())
        self.assertIs(caught.exception.__cause__, error)
        self.assertEqual(len(device.alpha_pv.get_timeouts), 2)


class ProcessTests(DeviceFixture, unittest.IsolatedAsyncioTestCase):
    async def test_success_in_each_mode_and_boundary_is_inclusive(self):
        cases = [("stddev", 0, (2, 2, 2), {}),
                 ("spread", 2, (1, 2, 3), {}),
                 ("reference", 1, (2, 3, 4),
                  {"reference": {"alpha": 1, "beta": 2, "theta": 3}})]
        for mode, band, values, extra in cases:
            with self.subTest(mode=mode):
                device = self.make_device(mode=mode, stable_reads=1,
                                          in_position_band=band, **extra)
                for pv, value in zip((device.alpha_pv, device.beta_pv,
                                      device.theta_pv), values):
                    pv.script(value)
                await device.process({})
                self.assertFalse(device.busy)
                self.assertEqual(device.alpha_pv.put_values, [])
                self.assertEqual(device.beta_pv.put_values, [])
                self.assertEqual(device.theta_pv.put_values, [])

    async def test_stable_count_resets_after_out_of_band_sample(self):
        device = self.make_device(mode="spread", in_position_band=0,
                                  stable_reads=2, poll_period=0.001)
        device.alpha_pv.script(0, 1, 0, 0)
        await device.process({})
        self.assertEqual(len(device.alpha_pv.get_timeouts), 4)
        self.assertFalse(device.busy)

    async def test_runtime_options_control_process_without_changing_defaults(self):
        device = self.make_device(stable_reads=3)
        await device.process({"stable_reads": 1})
        self.assertEqual(len(device.alpha_pv.get_timeouts), 1)
        self.assertEqual(device.stable_reads, 3)

    async def test_read_failures_recover_and_consecutive_counter_resets(self):
        device = self.make_device(stable_reads=2, read_retry_count=1,
                                  max_consecutive_read_failures=2, poll_period=0.001)
        device.alpha_pv.script(ValueError("first"), 0, ValueError("second"), 0)
        await device.process({})
        self.assertEqual(len(device.alpha_pv.get_timeouts), 4)
        self.assertFalse(device.busy)

    async def test_repeated_read_failure_clears_busy(self):
        device = self.make_device(read_retry_count=1,
                                  max_consecutive_read_failures=2, poll_period=0.001)
        device.alpha_pv.script(ValueError("offline"))
        with self.assertRaisesRegex(RuntimeError, "maximum consecutive PV read failures"):
            await device.process({})
        self.assertEqual(len(device.alpha_pv.get_timeouts), 2)
        self.assertFalse(device.busy)

    async def test_timeout_clears_busy(self):
        device = self.make_device(mode="spread", in_position_band=0, timeout=1e-12)
        device.alpha_pv.script(1)
        with self.assertRaises(TimeoutError):
            await device.process({})
        self.assertFalse(device.busy)

    async def test_nonfinite_metric_does_not_count_as_in_position(self):
        device = self.make_device(stable_reads=1, timeout=1e-12)
        device.alpha_pv.script(float("nan"))
        with self.assertRaises(TimeoutError):
            await device.process({})
        self.assertFalse(device.busy)

    async def test_invalid_runtime_options_do_not_enter_busy_state(self):
        device = self.make_device()
        for key in ("stable_reads", "log_every", "read_retry_count",
                    "max_consecutive_read_failures", "poll_period", "pv_read_timeout"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                await device.process({key: 0})
            self.assertFalse(device.busy)

    async def test_pause_release_and_watchdog(self):
        device = self.make_device(pause_pv="TEST:PAUSE", watchdog_pv="TEST:WD",
                                  stable_reads=1)
        device.pause_pv.script(1, 0)
        with patch.object(zero_order.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            await device.process({})
        sleep.assert_awaited_once_with(device.poll_period)
        self.assertEqual(device.watchdog_pv.put_values, [0, 0, 0])
        self.assertEqual(len(device.alpha_pv.get_timeouts), 1)

    async def test_stop_aborts_active_wait_and_next_process_clears_stop(self):
        device = self.make_device(mode="spread", in_position_band=0,
                                  stable_reads=1, poll_period=0.001, timeout=0)
        device.alpha_pv.script(1)
        entered_read = asyncio.Event()
        original_read = device._read_values_async

        async def signal_read(options):
            values = await original_read(options)
            entered_read.set()
            return values

        with patch.object(device, "_read_values_async", side_effect=signal_read):
            task = asyncio.create_task(device.process({}))
            try:
                await asyncio.wait_for(entered_read.wait(), timeout=2)
                self.assertTrue(device.busy)
                with self.assertRaisesRegex(RuntimeError, "already active"):
                    await device.process({})
                await device.stop()
                with self.assertRaisesRegex(RuntimeError, "aborted"):
                    await asyncio.wait_for(task, timeout=2)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(device.busy)
        device.alpha_pv.script(0)
        await device.process({"timeout": -1})
        self.assertFalse(device.busy)

    async def test_cancellation_clears_busy(self):
        device = self.make_device()

        async def cancel_read(_options):
            raise asyncio.CancelledError()

        with patch.object(device, "_read_values_async", side_effect=cancel_read), self.assertRaises(asyncio.CancelledError):
            await device.process({})

        self.assertFalse(device.busy)

    async def test_snapshot_values_configuration_and_pv_names(self):
        device = self.make_device(watchdog_pv="TEST:WD", pause_pv="TEST:PAUSE",
                                  reference={"alpha": 4, "beta": 5, "theta": 6})
        device.alpha_pv.script("1")
        device.beta_pv.script(2)
        device.theta_pv.script(3)
        snapshot = await device.snapshot()
        expected = {"alpha" : 1.0, "beta": 2.0, "theta": 3.0}
        self.assertEqual(snapshot, expected)


if __name__ == "__main__":
    unittest.main()
