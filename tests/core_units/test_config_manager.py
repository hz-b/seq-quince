"""Config loading, preset building, and persistence through real YAML files."""

import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from seq_quince import config_manager
from seq_quince.device_concrete.sim import SimDevice

from ._support import CoreTestCase


class SchedulerConfigManagerTests(CoreTestCase):
    def write_yaml(self, name, content):
        path = self.temp_dir / name
        path.write_text(yaml.safe_dump(content, sort_keys=False), encoding="utf-8")
        return path

    def make_manager(self, config=None, **kwargs):
        self.write_yaml("beamline.yaml", {} if config is None else config)
        return config_manager.SchedulerConfigManager(
            config_dir=str(self.temp_dir), **kwargs
        )

    def test_loading_renders_tokens_and_user_presets_override_config(self):
        self.write_yaml(
            "presets.yaml",
            {
                "presets": {
                    "shared": {"mono": {"position": 9}},
                    "user_only": {"mono": {"position": 7}},
                }
            },
        )
        manager = self.make_manager(
            {
                "devices": [{"name": "mono", "prefix": "${IOC}:MOTOR"}],
                "presets": {
                    "shared": {"mono": {"position": 1}},
                    "builtin": {"mono": {"position": 2}},
                },
                "macros": {"ready": "${IOC}:READY"},
                "sequence": {"preset": "shared"},
            },
            replacements={"IOC": "TEST"},
        )
        self.assertEqual(
            manager.get_device_configs(), [{"name": "mono", "prefix": "TEST:MOTOR"}]
        )
        self.assertEqual(
            manager.get_device_by_name("mono"), manager.get_device_configs()[0]
        )
        self.assertIsNone(manager.get_device_by_name("absent"))
        self.assertEqual(manager.get_preset("shared"), {"mono": {"position": 9}})
        self.assertEqual(manager.get_preset("builtin"), {"mono": {"position": 2}})
        self.assertEqual(manager.get_preset("user_only"), {"mono": {"position": 7}})
        self.assertEqual(manager.get_macros(), {"ready": "TEST:READY"})
        self.assertEqual(manager.get_sequence_config(), {"preset": "shared"})

    def test_optional_sections_and_missing_snapshot_use_empty_defaults(self):
        manager = self.make_manager()
        self.assertEqual(manager.get_device_configs(), [])
        self.assertEqual(manager.get_macros(), {})
        self.assertEqual(manager.get_sequence_config(), {})
        with self.assertRaisesRegex(ValueError, "Preset 'absent' not found"):
            manager.get_preset("absent")
        factory = Mock()
        self.assertEqual(manager.create_devices(factory), {})
        factory.create.assert_not_called()

    def test_directory_precedence_explicit_environment_then_helper(self):
        self.write_yaml("beamline.yaml", {})
        fallback = self.patch(
            config_manager,
            "get_scheduler_config_dir",
            Mock(return_value=str(self.temp_dir)),
        )
        with patch.dict(os.environ, {"SCHEDULER_CONFIG_DIR": "/unused"}):
            manager = config_manager.SchedulerConfigManager(
                config_dir=str(self.temp_dir)
            )
            self.assertEqual(manager.config_dir, str(self.temp_dir))
            fallback.assert_not_called()
        with patch.dict(os.environ, {"SCHEDULER_CONFIG_DIR": str(self.temp_dir)}):
            manager = config_manager.SchedulerConfigManager()
            self.assertEqual(manager.config_path, str(self.temp_dir / "beamline.yaml"))
            fallback.assert_not_called()
        self.write_yaml("other.yaml", {})
        with patch.dict(os.environ):
            os.environ.pop("SCHEDULER_CONFIG_DIR", None)
            manager = config_manager.SchedulerConfigManager(config_file="other.yaml")
        self.assertEqual(manager.config_path, str(self.temp_dir / "other.yaml"))
        fallback.assert_called_once_with()

    def test_fallback_keeps_config_filename_out_of_directory(self):
        directory = self.temp_dir / "scheduler_config" / "beamlines"
        directory.mkdir(parents=True)
        (directory / "other.yaml").write_text("devices: []\n", encoding="utf-8")
        with patch.dict(os.environ, {"SEQ_QUINCE_CONFIG_DIR": str(self.temp_dir)}):
            os.environ.pop("SCHEDULER_CONFIG_DIR", None)
            manager = config_manager.SchedulerConfigManager(config_file="other.yaml")
        self.assertEqual(manager.config_dir, str(directory))
        self.assertEqual(manager.config_path, str(directory / "other.yaml"))

    def test_device_factory_receives_merged_replacements(self):
        replacements = {"IOC": "CLI", "EXTRA": "cli-only"}
        manager = self.make_manager(
            {
                "devices": [
                    {
                        "name": "mono",
                        "replacements": {"IOC": "CONFIG", "LOCAL": "keep"},
                    },
                    {"name": "detector"},
                ]
            },
            replacements=replacements,
        )
        observed = []

        def create(config):
            observed.append(dict(config["replacements"]))
            return SimpleNamespace(name=config["name"])

        factory = Mock()
        factory.create.side_effect = create
        devices = manager.create_devices(factory)
        self.assertEqual(list(devices), ["mono", "detector"])
        self.assertEqual(
            observed,
            [
                {"IOC": "CLI", "LOCAL": "keep", "EXTRA": "cli-only"},
                {"IOC": "CLI", "EXTRA": "cli-only"},
            ],
        )
        self.assertEqual(factory.create.call_count, 2)
        self.assertEqual(replacements, {"IOC": "CLI", "EXTRA": "cli-only"})
        self.assertIsNot(manager.devices_cfg[1]["replacements"], replacements)

    def test_factory_failure_propagates(self):
        manager = self.make_manager({"devices": [{"name": "mono"}]})
        error = ValueError("invalid actuator configuration")
        factory = Mock()
        factory.create.side_effect = error
        with self.assertRaises(ValueError) as caught:
            manager.create_devices(factory)
        self.assertIs(caught.exception, error)

    def test_duplicate_device_names_fail_before_any_construction(self):
        manager = self.make_manager(
            {
                "devices": [
                    {"name": "mono", "type": "SimDevice"},
                    {"name": "detector", "type": "SimDevice"},
                    {"name": "mono", "type": "ToggleDevice"},
                ]
            }
        )
        factory = Mock()
        with self.assertRaisesRegex(ValueError, "Duplicate device name 'mono'"):
            manager.create_devices(factory)
        factory.create.assert_not_called()

    def test_unknown_preset_is_rejected_but_empty_preset_is_valid(self):
        manager = self.make_manager({"presets": {"empty": {}}})
        with self.assertRaisesRegex(ValueError, "Preset 'typo' not found"):
            manager.get_preset("typo")
        builder = Mock()
        with self.assertRaisesRegex(ValueError, "Preset 'typo' not found"):
            manager.build_sequence_from_preset(
                "typo", {}, build_sequence_func=builder
            )
        builder.assert_not_called()
        self.assertEqual(manager.get_preset("empty"), {})
        self.assertEqual(
            manager.build_sequence_from_preset("empty", {}, default_grouping="serial"),
            [],
        )
        parallel = manager.build_sequence_from_preset("empty", {})
        self.assertEqual(len(parallel), 1)
        self.assertEqual(parallel[0].description, "BARRIER")

    def test_real_builder_uses_preset_and_requested_grouping(self):
        manager = self.make_manager(
            {
                "presets": {
                    "home": {"mono": {"position": 3}, "detector": {"position": 4}},
                }
            }
        )
        devices = {"mono": SimDevice("mono"), "detector": SimDevice("detector")}
        parallel = manager.build_sequence_from_preset("home", devices)
        self.assertEqual([step.group_id for step in parallel], [0, 0, None])
        self.assertEqual(
            [step.device.name for step in parallel[:2]], ["mono", "detector"]
        )
        serial = manager.build_sequence_from_preset(
            "home", devices, default_grouping="serial"
        )
        self.assertEqual([step.group_id for step in serial], [0, 1])
        self.assertEqual(
            [step.pose for step in serial], [{"position": 3}, {"position": 4}]
        )

    def test_injected_builder_and_null_preset_validation(self):
        manager = self.make_manager(
            {
                "presets": {
                    "home": {"mono": {"position": 3}},
                    "invalid": None,
                }
            }
        )
        devices = {"mono": object()}
        result = object()
        builder = Mock(return_value=result)
        self.assertIs(
            manager.build_sequence_from_preset(
                "home",
                devices,
                default_grouping="serial",
                build_sequence_func=builder,
            ),
            result,
        )
        builder.assert_called_once_with(
            devices, {"mono": {"position": 3}}, default_grouping="serial"
        )
        with self.assertRaisesRegex(ValueError, "Preset 'invalid' not found"):
            manager.build_sequence_from_preset(
                "invalid", devices, build_sequence_func=builder
            )
        self.assertEqual(builder.call_count, 1)

    def test_update_preserves_other_values_and_rejects_nonmapping_entry(self):
        manager = self.make_manager(
            {
                "presets": {
                    "home": {"mono": {"position": 3, "velocity": 2}, "bad": 1},
                }
            }
        )
        manager.update_preset_item("home", "mono", {"position": 8})
        manager.update_preset_item("new", "detector", {"position": 5})
        self.assertEqual(
            manager.get_preset("home")["mono"], {"position": 8, "velocity": 2}
        )
        self.assertEqual(manager.get_preset("new"), {"detector": {"position": 5}})
        self.assertEqual(
            manager.config["presets"]["new"], {"detector": {"position": 5}}
        )
        with self.assertRaisesRegex(
            TypeError, "Preset entry for bad in 'home' must be a dict"
        ):
            manager.update_preset_item("home", "bad", {"position": 5})

    def test_snapshot_delegation_and_config_save_round_trip(self):
        manager = self.make_manager(
            {
                "devices": [{"name": "mono"}],
                "presets": {"home": {"mono": {"position": 1}}},
            }
        )
        structure = {"mono": {"position": 8}}
        manager.save_snapshot("measured", structure)
        presets = yaml.safe_load(
            (self.temp_dir / "presets.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(presets["presets"]["measured"], structure)
        manager.update_preset_item("home", "mono", {"position": 2})
        output = self.temp_dir / "saved.yaml"
        manager.save_config(str(output))
        self.assertEqual(
            yaml.safe_load(output.read_text(encoding="utf-8")), manager.config
        )
        manager.save_config()
        self.assertEqual(
            yaml.safe_load(
                (self.temp_dir / "beamline.yaml").read_text(encoding="utf-8")
            ),
            manager.config,
        )

    def test_missing_input_and_unwritable_destination_propagate(self):
        with self.assertRaises(FileNotFoundError):
            config_manager.SchedulerConfigManager(config_dir=str(self.temp_dir))
        manager = self.make_manager()
        with self.assertRaises(OSError):
            manager.save_config(str(self.temp_dir / "missing" / "saved.yaml"))
