from types import SimpleNamespace
from unittest.mock import Mock

from seq_quince.device_concrete import ophyd_motor

from ._support import DeviceTestCase


class OphydMotorDeviceTests(DeviceTestCase):
    def setUp(self):
        super().setUp()
        self.motor = SimpleNamespace(
            position=2.5, moving=False, move=Mock(), stop=Mock(), destroy=Mock()
        )
        constructor = self.patch(
            ophyd_motor, "EpicsMotor", Mock(return_value=self.motor)
        )
        self.device = ophyd_motor.OphydMotorDevice.from_config_dict(
            {"name": "motor", "pv_prefix": "TEST:M1"}
        )
        constructor.assert_called_once_with("TEST:M1", name="motor")
    
    def test_move_waits_and_clears_local_busy(self):
        def move(position, *, wait):
            self.assertEqual(position, 0)
            self.assertTrue(wait)
            self.assertTrue(self.device.busy)

        self.motor.move.side_effect = move

        self.run_async(self.device.process({"position": 0}))

        self.motor.move.assert_called_once_with(0, wait=True)
        self.assertFalse(self.device.busy)

    def test_missing_target_never_dispatches(self):
        with self.assertRaisesRegex(ValueError, "position"):
            self.run_async(self.device.process({}))
        self.motor.move.assert_not_called()
        # Readiness stays conservative until an async path samples hardware.
        self.run_async(self.device.snapshot())
        self.assertFalse(self.device.busy)

    def test_stop_calls_driver_and_clears_local_busy(self):
        self.motor.move.side_effect = RuntimeError("drive fault")
        with self.assertRaisesRegex(RuntimeError, "drive fault"):
            self.run_async(self.device.process({"position": 5}))
        self.run_async(self.device.stop())
        self.motor.stop.assert_called_once_with()
        self.assertFalse(self.device.busy)
