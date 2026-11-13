"""Device checker orchestration with real CLI/YAML parsing and fake devices."""

import io
import os
import runpy
import sys
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import AsyncMock, Mock, PropertyMock, call, patch

import yaml

from seq_quince import scheduler_check_devices as checker

from ._support import CoreTestCase


class CheckedDevice:
    def __init__(self, name="device"):
        self.name = name
        self.process = AsyncMock()
        self.snapshot = AsyncMock(return_value={"position": 12.5})
        self.stop = AsyncMock()
        self.close = AsyncMock()

    @property
    def busy(self):
        return False


class SchedulerCheckDevicesTests(CoreTestCase):
    def test_check_device_constructs_moves_reads_and_stops_in_order(self):
        device = CheckedDevice()
        factory = Mock()
        factory.from_config_dict.return_value = device
        operations = Mock()
        for name, operation in (
            ("construct", factory.from_config_dict),
            ("process", device.process),
            ("snapshot", device.snapshot),
            ("stop", device.stop),
            ("close", device.close),
        ):
            operations.attach_mock(operation, name)
        config = {"name": "device", "pv": "TEST:MOTOR"}
        pose = {"position": 10}
        output = io.StringIO()
        with redirect_stdout(output):
            self.run_async(checker._check_device(factory, config, pose))
        self.assertEqual(
            operations.mock_calls,
            [call.construct(config), call.process(pose), call.snapshot(), call.stop(), call.close()],
        )
        device.process.assert_awaited_once_with(pose)
        device.snapshot.assert_awaited_once_with()
        device.stop.assert_awaited_once_with()
        self.assertEqual(
            output.getvalue().splitlines(), ["{'position': 12.5}", "Busy? False"]
        )

    def test_each_device_stage_logs_errors_without_escaping(self):
        for stage in ("constructor", "process", "snapshot", "busy", "stop"):
            with self.subTest(stage=stage):
                device = CheckedDevice()
                factory = Mock()
                factory.from_config_dict.return_value = device
                error = RuntimeError("failure at " + stage)
                if stage == "constructor":
                    factory.from_config_dict.side_effect = error
                elif stage != "busy":
                    getattr(device, stage).side_effect = error
                busy = PropertyMock(return_value=False)
                if stage == "busy":
                    busy.side_effect = error
                output = io.StringIO()
                with patch.object(CheckedDevice, "busy", busy), redirect_stdout(
                    output
                ), self.assertLogs(level="ERROR") as logs:
                    result = self.run_async(
                        checker._check_device(factory, {"name": "test"}, {})
                    )
                self.assertIsNone(result)
                self.assertIn("failure at " + stage, "\n".join(logs.output))
                factory.from_config_dict.assert_called_once_with({"name": "test"})
                if stage == "constructor":
                    device.process.assert_not_awaited()
                if stage in ("constructor", "process"):
                    device.snapshot.assert_not_awaited()
                if stage != "stop":
                    device.stop.assert_not_awaited()

    def _main_with_real_yaml(self, environment_override, fail_first):
        checks = [
            ("simulation", "SimDevice", {"position": 100.0}),
            ("mono_energy", "ActuatorDevice", {"position": 100.0}),
            ("motor_record", "OphydMotorDevice", {"position": 10.0}),
            ("configure_scan", "SequenceDevice", {}),
            ("diode", "ToggleDevice", {}),
            ("energy_scan", "ScanDevice", {}),
        ]
        config = {
            "devices": [
                {
                    "name": name,
                    "pv": "${IOC}:" + name,
                    "replacements": {"IOC": "FROM_YAML", "KEEP": "retained"},
                }
                for name, _, _ in checks
            ]
        }
        (self.temp_dir / "beamline.yaml").write_text(
            yaml.safe_dump(config), encoding="utf-8"
        )
        factories = {}
        devices = {}
        for name, attribute, _ in checks:
            device = CheckedDevice(name)
            device.snapshot.return_value = {"position": len(name)}
            factory = Mock()
            factory.from_config_dict.return_value = device
            self.patch(checker, attribute, factory)
            factories[name] = factory
            devices[name] = device
        if fail_first:
            devices["simulation"].process.side_effect = RuntimeError(
                "simulation failed"
            )

        fallback = self.temp_dir / "unused" if environment_override else self.temp_dir
        directory = self.patch(
            checker, "get_scheduler_config_dir", Mock(return_value=str(fallback))
        )
        self.patch(
            sys,
            "argv",
            [
                "scheduler-check-devices",
                "--config",
                "beamline",
                "--replace",
                "IOC=FROM_CLI",
                "EXTRA=two=parts",
            ],
        )
        output = io.StringIO()
        with patch.dict(os.environ), redirect_stdout(output):
            os.environ.pop("SCHEDULER_CONFIG_DIR", None)
            if environment_override:
                os.environ["SCHEDULER_CONFIG_DIR"] = str(self.temp_dir)
            result = self.run_async(checker.main())

        if environment_override:
            directory.assert_not_called()
        else:
            directory.assert_called_once_with("beamline")
        self.assertIsNone(result)
        for name, _, pose in checks:
            factory, device = factories[name], devices[name]
            factory.from_config_dict.assert_called_once()
            actual_config = factory.from_config_dict.call_args.args[0]
            self.assertEqual(actual_config["name"], name)
            self.assertEqual(actual_config["pv"], "FROM_CLI:" + name)
            self.assertEqual(
                actual_config["replacements"],
                {
                    "IOC": "FROM_CLI",
                    "KEEP": "retained",
                    "EXTRA": "two=parts",
                },
            )
            device.process.assert_awaited_once_with(pose)
            if name == "simulation" and fail_first:
                device.snapshot.assert_not_awaited()
                device.stop.assert_not_awaited()
            else:
                device.snapshot.assert_awaited_once_with()
                device.stop.assert_awaited_once_with()
                self.assertIn(str({"position": len(name)}), output.getvalue())
        self.assertEqual(output.getvalue().count("Busy? False"), 5 if fail_first else 6)

    def test_main_checks_all_six_device_types_with_cli_replacement_precedence(self):
        self._main_with_real_yaml(environment_override=False, fail_first=False)

    def test_main_honors_environment_directory_and_continues_after_one_failure(self):
        with self.assertLogs(level="ERROR") as logs:
            self._main_with_real_yaml(environment_override=True, fail_first=True)
        self.assertIn("simulation failed", "\n".join(logs.output))

    def test_invalid_cli_stops_before_loading_configuration_or_creating_devices(self):
        self.patch(sys, "argv", ["scheduler-check-devices"])
        loader = self.patch(checker, "yaml_loader", Mock())
        with redirect_stderr(io.StringIO()) as errors, self.assertRaises(
            SystemExit
        ) as caught:
            self.run_async(checker.main())
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--config", errors.getvalue())
        loader.assert_not_called()

    def test_run_wrapper_awaits_main_using_real_asyncio_run(self):
        main = self.patch(checker, "main", AsyncMock(return_value=None))
        self.assertIsNone(checker.run())
        main.assert_awaited_once_with()

    def test_script_entry_point_rejects_missing_config_before_hardware_access(self):
        self.patch(sys, "argv", ["scheduler-check-devices"])
        with redirect_stderr(io.StringIO()) as errors, self.assertRaises(
            SystemExit
        ) as caught:
            runpy.run_path(checker.__file__, run_name="__main__")
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--config", errors.getvalue())
