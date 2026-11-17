"""Parameter conversion, base defaults, factory dispatch, and option lookup."""

from unittest.mock import patch

from seq_quince.device.device_abs import DeviceABC, get_param
from seq_quince.device.factory import DEVICE_REGISTRY, DeviceFactory
from seq_quince.device.lifecycle import MotionBusyMixin
from seq_quince.device_concrete._option_lookup import first_present, value_from_pose
from seq_quince.device_concrete.sim import SimDevice

from ._support import DeviceTestCase


class DeviceContractTests(DeviceTestCase):
    def test_get_param_preserves_or_converts_values(self):
        cases = [
            ({}, None, None),
            ({}, 3.5, 3.5),
            ({"x": 0}, 7, 0),
            ({"x": "2.5"}, 7, 2.5),
        ]
        for config, default, expected in cases:
            with self.subTest(config=config, default=default):
                self.assertEqual(
                    get_param(config, "x", float, default=default), expected
                )

    def test_get_param_rejects_missing_and_unconvertible_values(self):
        for config in ({}, {"x": None}):
            with self.subTest(config=config), self.assertRaisesRegex(
                ValueError, "Missing required parameter: 'x'"
            ):
                get_param(config, "x", float, required=True)
        with self.assertRaisesRegex(TypeError, "'x' expects type float"):
            get_param({"x": "invalid"}, "x", float)

    def test_base_contract_and_default_capabilities(self):
        with self.assertRaisesRegex(TypeError, "abstract"):
            DeviceABC()
        device = SimDevice("sim")
        self.assertIsNone(device.get_capability("unknown"))
        self.assertTrue(device.supports_rollback())
        self.assertIsNone(device.scheduler_timeout({"timeout": 1}))

    def test_registry_lists_core_devices_and_returns_a_copy(self):
        expected_core = {
            "ActuatorDevice",
            "HexapodDevice",
            "MonoZeroOrderDevice",
            "OphydMotorDevice",
            "ScanDevice",
            "SequenceDevice",
            "SimDevice",
            "ToggleDevice",
            "WaitConditionDevice",
        }
        available = DeviceFactory.available_types()
        registered = set(available)
        self.assertTrue(
            expected_core.issubset(registered),
            f"Missing core device types: {sorted(expected_core - registered)}",
        )
        available.clear()
        self.assertEqual(set(DeviceFactory.available_types()), registered)
        self.assertTrue(
            all(issubclass(cls, DeviceABC) for cls in DEVICE_REGISTRY.values())
        )

    def test_motion_adapters_share_one_busy_definition(self):
        for name in ("ActuatorDevice", "HexapodDevice", "OphydMotorDevice"):
            with self.subTest(device=name):
                device_cls = DEVICE_REGISTRY[name]
                self.assertNotIn("busy", vars(device_cls))
                self.assertIs(device_cls.busy, MotionBusyMixin.busy)

    def test_factory_creation_and_registration(self):
        config = {"type": "SimDevice", "name": "sim", "delay": "0"}
        device = DeviceFactory.create(config)
        self.assertIsInstance(device, SimDevice)
        self.assertEqual(device.name, "sim")
        with patch.dict(DEVICE_REGISTRY, {"Custom": DEVICE_REGISTRY["ActuatorDevice"]}):
            DeviceFactory.register("Custom", SimDevice)
            self.assertIsInstance(
                DeviceFactory.create(dict(config, type="Custom")), SimDevice
            )

    def test_factory_rejects_missing_and_unknown_types(self):
        for config, message in [
            ({}, "missing required"),
            ({"type": "?"}, "Unknown device type"),
        ]:
            with self.subTest(config=config), self.assertRaisesRegex(
                ValueError, message
            ):
                DeviceFactory.create(config)

    def test_option_precedence_and_explicit_false_values(self):
        cases = [
            ({"key": 0, "alias": 1}, {"key": 2}, 0),
            ({"alias": False}, {"key": 2}, False),
            ({"key": None}, {"key": 2}, None),
            ({}, {"key": 2, "alias": 3}, 2),
            ({}, {"alias": 3}, 3),
            ({}, {}, 4),
        ]
        for primary, secondary, expected in cases:
            with self.subTest(primary=primary, secondary=secondary):
                self.assertEqual(
                    value_from_pose(primary, secondary, "key", 4, ("alias",)), expected
                )
                self.assertEqual(
                    first_present(primary, secondary, keys=("key", "alias"), default=4),
                    expected,
                )
