from seq_quince.device_concrete.toggle import ToggleDevice

from ._support import DeviceTestCase


class ToggleDeviceTests(DeviceTestCase):
    def setUp(self):
        super().setUp()
        self.init_pvs()
        self.device = ToggleDevice.from_config_dict(
            {
                "name": "power",
                "readback_pv": "TEST:RBV",
                "toggle_pv": "TEST:TOGGLE",
                "setpoint": "1",
                "toggle_val": "2",
            }
        )
        self.readback = self.pvs["TEST:RBV"]
        self.toggle = self.pvs["TEST:TOGGLE"]

    def test_toggles_only_when_needed_and_pose_overrides_default(self):
        self.readback.get.return_value = 1
        self.run_async(self.device.process({}))
        self.toggle.put.assert_not_called()
        self.run_async(self.device.process({"setpoint": 0}))
        self.toggle.put.assert_called_once_with(2.0)
        self.assertEqual(self.device.setpoint, 1.0)
        self.assertFalse(self.device.busy)
        self.assertEqual(self.run_async(self.device.snapshot()), {"setpoint": 1})
        self.run_async(self.device.stop())
        self.assertFalse(self.device.busy)

    def test_snapshot_replay_uses_saved_readback_instead_of_configured_setpoint(self):
        self.readback.get.return_value = 0
        snapshot = self.run_async(self.device.snapshot())
        self.assertEqual(snapshot, {"setpoint": 0})
        self.readback.get.return_value = 1
        self.run_async(self.device.process(snapshot))
        self.toggle.put.assert_called_once_with(2.0)
        self.readback.get.return_value = 0
        self.run_async(self.device.process(snapshot))
        self.toggle.put.assert_called_once_with(2.0)
        self.assertEqual(self.device.setpoint, 1.0)

    def test_failed_put_and_read_are_logged_and_busy_is_cleared(self):
        self.toggle.put.return_value = False
        with self.assertLogs(level="ERROR") as logs:
            self.run_async(self.device.process({}))
        self.assertIn("Failed to toggle", "\n".join(logs.output))
        self.assertFalse(self.device.busy)
        self.readback.get.side_effect = RuntimeError("offline")
        with self.assertLogs(level="ERROR") as logs:
            self.run_async(self.device.process({}))
            with self.assertRaisesRegex(RuntimeError, "Could not get value"):
                self.run_async(self.device.snapshot())
        self.assertIn("offline", "\n".join(logs.output))
        self.assertFalse(self.device.busy)
