"""Resource ownership and surviving executor work at mocked driver boundaries."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from seq_quince.device.lifecycle import DeviceScope
from seq_quince.device_concrete import (
    actuator,
    hexapod,
    mono_zero_order,
    ophyd_motor,
    sequence,
    toggle,
    wait_condition,
)

from ._support import DeviceTestCase


class DeviceResourceTests(DeviceTestCase):
    def setUp(self):
        super().setUp()
        self.init_actuators()
        self.init_pvs()

    def test_actuator_closes_once_outside_loop_without_stopping(self):
        device = actuator.ActuatorDevice("axis", {"pv": "TEST:AXIS"})
        driver = self.drivers[0]
        loop_thread = threading.get_ident()
        threads = []
        driver.close.side_effect = lambda: threads.append(threading.get_ident())

        async def scenario():
            await asyncio.gather(device.close(), device.close())
            await device.close()

        self.run_async(scenario())
        driver.close.assert_called_once_with()
        driver.stop.assert_not_called()
        self.assertNotEqual(threads, [loop_thread])

    def test_closed_adapter_rejects_new_operations(self):
        device = actuator.ActuatorDevice("axis", {"pv": "TEST:AXIS"})
        self.run_async(device.close())
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.run_async(device.process({"position": 1}))
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.run_async(device.snapshot())
        self.drivers[0].run_move.assert_not_called()

    def test_ophyd_uses_destroy_without_stopping_or_raw_pv_disconnect(self):
        motor = SimpleNamespace(destroy=Mock(), stop=Mock())
        self.patch(ophyd_motor, "EpicsMotor", Mock(return_value=motor))
        device = ophyd_motor.OphydMotorDevice("motor", "TEST:M1")
        self.run_async(device.close())
        self.run_async(device.close())
        motor.destroy.assert_called_once_with()
        motor.stop.assert_not_called()

    def test_hexapod_releases_all_axes_and_owned_run_after_pv(self):
        run_after = SimpleNamespace(disconnect=Mock())
        self.patch(hexapod, "PV", Mock(return_value=run_after))
        device = hexapod.HexapodDevice("hex", {
            "axes": {axis: {"pv": "TEST:" + axis} for axis in ("Tx", "Ty", "Tz", "Rx", "Ry", "Rz")},
            "run_after_pv": "TEST:RUN", "run_after_value": 1,
        })
        self.run_async(device.close())
        for driver in self.drivers:
            driver.close.assert_called_once_with()
            driver.stop.assert_not_called()
        run_after.disconnect.assert_called_once_with()

    def test_partial_hexapod_construction_keeps_successful_axes_owned(self):
        create = self.driver_factory.side_effect

        def fail_third(config):
            if len(self.drivers) == 2:
                raise ValueError("third actuator failed")
            return create(config)

        self.driver_factory.side_effect = fail_third

        async def scenario():
            async with DeviceScope():
                hexapod.HexapodDevice("hex", {
                    "axes": {axis: {"pv": "TEST:" + axis} for axis in ("Tx", "Ty", "Tz", "Rx", "Ry", "Rz")},
                })

        with self.assertRaisesRegex(ValueError, "third actuator failed"):
            self.run_async(scenario())
        self.assertEqual(len(self.drivers), 2)
        for driver in self.drivers:
            driver.close.assert_called_once_with()

    def test_partial_toggle_construction_closes_first_pv(self):
        first = self.pv_factory("TEST:RB")
        self.patch(toggle, "EpicsPV", Mock(side_effect=[first, ConnectionError("second PV failed")]))

        async def scenario():
            async with DeviceScope():
                toggle.ToggleDevice("toggle", "TEST:RB", 1, "TEST:CMD")

        with self.assertRaisesRegex(ConnectionError, "second PV failed"):
            self.run_async(scenario())
        first.close.assert_called_once_with()

    def test_toggle_and_sequence_release_every_owned_pv(self):
        toggle_device = toggle.ToggleDevice("toggle", "TEST:RB", 1, "TEST:CMD")
        sequence_device = sequence.SequenceDevice("sequence", {
            "target": {"pv": "TEST:SET", "desired_pv": "TEST:DESIRED", "queue_delay": 0},
        })

        async def scenario():
            await sequence_device.process({})
            await toggle_device.close()
            await sequence_device.close()

        self.run_async(scenario())
        self.assertEqual(set(self.pvs), {"TEST:RB", "TEST:CMD", "TEST:SET", "TEST:DESIRED"})
        for pv in self.pvs.values():
            pv.close.assert_called_once_with()

    def test_zero_order_and_wait_release_cached_and_optional_pvs(self):
        self.patch(mono_zero_order, "EpicsPV", self.pv_factory)
        zero = mono_zero_order.MonoZeroOrderDevice(
            "zero", "TEST:A", "TEST:B", "TEST:T", "TEST:WD", "TEST:PAUSE"
        )
        wait = wait_condition.WaitConditionDevice("wait", pv="TEST:READY", value=0)

        async def scenario():
            await wait.process({})
            await zero.close()
            await wait.close()

        self.run_async(scenario())
        self.assertEqual(len(self.pvs), 6)
        for pv in self.pvs.values():
            pv.close.assert_called_once_with()

    def test_close_waits_for_worker_after_process_waiter_is_cancelled(self):
        device = actuator.ActuatorDevice("axis", {"pv": "TEST:AXIS"})
        driver = self.drivers[0]
        release = threading.Event()

        async def scenario():
            started = asyncio.Event()
            loop = asyncio.get_running_loop()

            def move(*args):
                loop.call_soon_threadsafe(started.set)
                if not release.wait(2):
                    raise TimeoutError("test worker not released")

            driver.run_move.side_effect = move
            operation = asyncio.create_task(device.process({"position": 1}))
            closing = None
            try:
                await asyncio.wait_for(started.wait(), 1)
                operation.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await operation
                closing = asyncio.create_task(device.close())
                await asyncio.sleep(0.01)
                driver.close.assert_not_called()
                self.assertFalse(closing.done())
            finally:
                release.set()
                if closing is not None:
                    await asyncio.wait_for(closing, 1)
                await asyncio.gather(operation, return_exceptions=True)

        self.run_async(scenario())
        driver.close.assert_called_once_with()
