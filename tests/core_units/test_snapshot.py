"""Round-trip snapshot data without mocking YAML or file writes."""

import contextlib
import io
import logging
import os
import stat
from unittest.mock import Mock, patch

import yaml

from seq_quince import snapshot

from ._support import CoreTestCase


class SnapshotTests(CoreTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(snapshot.log.setLevel, snapshot.log.level)

    def manager(self, **kwargs):
        return snapshot.SnapshotManager(config_dir=str(self.temp_dir), **kwargs)

    def load(self, manager):
        with contextlib.redirect_stdout(io.StringIO()):
            return manager.load_snapshot()

    def test_directory_precedence_and_custom_filename(self):
        fallback = self.patch(
            snapshot, "get_scheduler_config_dir", Mock(return_value="/fallback")
        )
        with patch.dict(os.environ, {"SCHEDULER_CONFIG_DIR": "/environment"}):
            explicit = snapshot.SnapshotManager(
                config_dir=str(self.temp_dir),
                preset_file="custom.yaml",
                log_level=logging.DEBUG,
            )
            self.assertEqual(explicit.preset_path, str(self.temp_dir / "custom.yaml"))
            self.assertEqual(snapshot.log.level, logging.DEBUG)
            self.assertEqual(snapshot.SnapshotManager().config_dir, "/environment")
            fallback.assert_not_called()
        with patch.dict(os.environ, {}, clear=True):
            default = snapshot.SnapshotManager()
        self.assertEqual(default.preset_path, os.path.join("/fallback", "presets.yaml"))
        fallback.assert_called_once_with()

    def test_missing_file_loads_empty_and_save_creates_a_round_trip(self):
        manager = self.manager()
        self.assertEqual(manager.load_snapshot(), {})
        pose = {"theta": {"position": 1.5}, "gap": {"position": 3.0}}
        with self.assertLogs(snapshot.log, level="WARNING") as logs:
            manager.save_snapshot("initial", pose)
        self.assertIn("Creating a new one", "\n".join(logs.output))
        self.assertEqual(self.load(manager), {"presets": {"initial": pose}})
        with open(manager.preset_path, encoding="utf-8") as handle:
            self.assertEqual(yaml.safe_load(handle), {"presets": {"initial": pose}})

    def test_save_preserves_other_presets_and_metadata_and_replaces_named_pose(self):
        manager = self.manager()
        path = self.temp_dir / "presets.yaml"
        path.write_text(
            "description: commissioning\npresets:\n  keep: {x: {position: 1}}\n  replace: {old: 2}\n",
            encoding="utf-8",
        )
        manager.save_snapshot("replace", {"y": {"position": 5}})
        manager.save_snapshot("new", {"z": {"position": 6}})
        self.assertEqual(
            self.load(manager),
            {
                "description": "commissioning",
                "presets": {
                    "keep": {"x": {"position": 1}},
                    "replace": {"y": {"position": 5}},
                    "new": {"z": {"position": 6}},
                },
            },
        )

    def test_save_handles_empty_yaml_and_documents_without_presets(self):
        path = self.temp_dir / "presets.yaml"
        for initial, metadata in (
            ("", {}),
            ("description: scan\n", {"description": "scan"}),
        ):
            with self.subTest(initial=initial):
                path.write_text(initial, encoding="utf-8")
                manager = self.manager()
                manager.save_snapshot("new", {"x": {"position": 0}})
                expected = dict(metadata, presets={"new": {"x": {"position": 0}}})
                self.assertEqual(self.load(manager), expected)

    def test_load_applies_real_template_replacements(self):
        path = self.temp_dir / "presets.yaml"
        path.write_text(
            "presets:\n  initial:\n    ${DEVICE}:\n      position: {{ POSITION }}\n",
            encoding="utf-8",
        )
        manager = self.manager(replacements={"DEVICE": "theta", "POSITION": "2.5"})
        self.assertEqual(
            self.load(manager), {"presets": {"initial": {"theta": {"position": 2.5}}}}
        )

    def test_malformed_existing_yaml_is_not_overwritten(self):
        path = self.temp_dir / "presets.yaml"
        invalid = "presets: [broken\n"
        path.write_text(invalid, encoding="utf-8")
        with self.assertRaises(yaml.YAMLError):
            self.manager().save_snapshot("new", {"x": {"position": 1}})
        self.assertEqual(path.read_text(encoding="utf-8"), invalid)

    def test_invalid_destination_propagates_write_error(self):
        manager = snapshot.SnapshotManager(config_dir=str(self.temp_dir / "missing"))
        with self.assertRaises(FileNotFoundError):
            manager.save_snapshot("new", {"x": {"position": 1}})

    def test_atomic_creation_and_replacement_use_closed_complete_unique_files(self):
        manager = self.manager()
        path = self.temp_dir / "presets.yaml"
        temporary_files = []
        create_temporary = snapshot.tempfile.NamedTemporaryFile
        replace = os.replace
        expected_before = None
        pose = {"x": {"position": 1}}

        def track_temporary(*args, **kwargs):
            temporary = create_temporary(*args, **kwargs)
            temporary.flush = Mock(wraps=temporary.flush)
            temporary_files.append(temporary)
            return temporary

        def check_and_replace(source, destination):
            temporary = temporary_files[-1]
            self.assertTrue(temporary.closed)
            temporary.flush.assert_called_with()
            self.assertEqual(os.path.dirname(source), str(self.temp_dir))
            self.assertEqual(destination, manager.preset_path)
            with open(source, encoding="utf-8") as handle:
                self.assertEqual(yaml.safe_load(handle), {"presets": {"new": pose}})
            if expected_before is None:
                self.assertFalse(path.exists())
            else:
                self.assertEqual(path.read_bytes(), expected_before)
            replace(source, destination)

        with patch.object(snapshot.tempfile, "NamedTemporaryFile", track_temporary), \
                patch.object(snapshot.os, "replace", side_effect=check_and_replace) as swap:
            manager.save_snapshot("new", pose)
            expected_before = path.read_bytes()
            pose = {"x": {"position": 2}}
            manager.save_snapshot("new", pose)

        self.assertEqual(swap.call_count, 2)
        self.assertNotEqual(temporary_files[0].name, temporary_files[1].name)
        self.assertEqual(list(self.temp_dir.iterdir()), [path])
        self.assertEqual(self.load(manager), {"presets": {"new": pose}})

    def test_replacement_preserves_existing_file_permissions(self):
        path = self.temp_dir / "presets.yaml"
        path.write_text("presets: {}\n", encoding="utf-8")
        path.chmod(0o640)
        original_mode = stat.S_IMODE(path.stat().st_mode)
        self.manager().save_snapshot("new", {"x": {"position": 1}})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), original_mode)

    def test_serialization_failure_preserves_destination_and_removes_temporary_file(self):
        path = self.temp_dir / "presets.yaml"
        original = b"description: untouched\npresets: {}\n"
        for exists in (False, True):
            with self.subTest(existing_destination=exists):
                if exists:
                    path.write_bytes(original)
                with patch.object(snapshot.os, "replace") as replace, \
                        self.assertRaises(yaml.representer.RepresenterError):
                    self.manager().save_snapshot("bad", {"x": {"position": object()}})
                replace.assert_not_called()
                if exists:
                    self.assertEqual(path.read_bytes(), original)
                else:
                    self.assertFalse(path.exists())
                self.assertEqual(list(self.temp_dir.iterdir()), [path] if exists else [])

    def test_write_flush_close_and_replace_errors_leave_destination_intact_and_clean_up(self):
        path = self.temp_dir / "presets.yaml"
        original = b"description: untouched\npresets: {}\n"
        create_temporary = snapshot.tempfile.NamedTemporaryFile
        for operation in ("write", "flush", "close", "replace"):
            for exists in (False, True):
                with self.subTest(operation=operation, existing_destination=exists):
                    if path.exists():
                        path.unlink()
                    if exists:
                        path.write_bytes(original)
                    error = OSError("injected " + operation + " failure")

                    def failing_temporary(*args, operation=operation, error=error, **kwargs):
                        temporary = create_temporary(*args, **kwargs)
                        if operation == "write":
                            # Fail after a real partial write to the temporary file.
                            temporary.write("partial YAML\n")
                            temporary.write = Mock(side_effect=error)
                        elif operation == "flush":
                            temporary.flush = Mock(side_effect=error)
                        elif operation == "close":
                            close = temporary.close

                            def close_then_fail():
                                close()
                                raise error

                            temporary.close = Mock(side_effect=close_then_fail)
                        return temporary

                    replace = Mock(side_effect=error)
                    with patch.object(snapshot.tempfile, "NamedTemporaryFile", failing_temporary), \
                            patch.object(snapshot.os, "replace", replace), \
                            self.assertRaises(OSError) as caught:
                        self.manager().save_snapshot("new", {"x": {"position": 1}})

                    self.assertIs(caught.exception, error)
                    if operation == "replace":
                        replace.assert_called_once()
                    else:
                        replace.assert_not_called()
                    if exists:
                        self.assertEqual(path.read_bytes(), original)
                    else:
                        self.assertFalse(path.exists())
                    self.assertEqual(list(self.temp_dir.iterdir()), [path] if exists else [])

    def test_close_error_does_not_mask_serialization_or_write_failure(self):
        path = self.temp_dir / "presets.yaml"
        original = b"description: untouched\npresets: {}\n"
        path.write_bytes(original)
        create_temporary = snapshot.tempfile.NamedTemporaryFile
        for operation in ("serialization", "write"):
            with self.subTest(operation=operation):
                error = yaml.YAMLError("serialization failed") if operation == "serialization" \
                    else OSError("write failed")
                temporary_files = []

                def failing_temporary(
                    *args, operation=operation, error=error,
                    temporary_files=temporary_files, **kwargs,
                ):
                    temporary = create_temporary(*args, **kwargs)
                    temporary.write("partial YAML\n")
                    if operation == "write":
                        temporary.write = Mock(side_effect=error)
                    close = temporary.close

                    def close_then_fail():
                        close()
                        raise OSError("close also failed")

                    temporary.close = Mock(side_effect=close_then_fail)
                    temporary_files.append(temporary)
                    return temporary

                def failing_dump(data, stream, error=error, **kwargs):
                    stream.write("more partial YAML\n")
                    raise error

                with patch.object(snapshot.tempfile, "NamedTemporaryFile", failing_temporary), \
                        patch.object(snapshot.yaml, "safe_dump", side_effect=failing_dump), \
                        patch.object(snapshot.os, "replace") as replace, \
                        self.assertRaises(type(error)) as caught:
                    self.manager().save_snapshot("new", {"x": {"position": 1}})

                self.assertIs(caught.exception, error)
                self.assertTrue(temporary_files[0].closed)
                replace.assert_not_called()
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(list(self.temp_dir.iterdir()), [path])

    def test_temporary_creation_error_preserves_destination(self):
        path = self.temp_dir / "presets.yaml"
        original = b"presets: {}\n"
        path.write_bytes(original)
        error = OSError("cannot create temporary file")
        with patch.object(snapshot.tempfile, "NamedTemporaryFile", side_effect=error), \
                self.assertRaises(OSError) as caught:
            self.manager().save_snapshot("new", {"x": {"position": 1}})
        self.assertIs(caught.exception, error)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.temp_dir.iterdir()), [path])

    def test_cleanup_error_does_not_mask_original_failure(self):
        path = self.temp_dir / "presets.yaml"
        original = b"presets: {}\n"
        path.write_bytes(original)
        error = OSError("replace failed")
        with patch.object(snapshot.os, "replace", side_effect=error), \
                patch.object(snapshot.os, "unlink", side_effect=PermissionError("cleanup denied")), \
                self.assertRaises(OSError) as caught:
            self.manager().save_snapshot("new", {"x": {"position": 1}})
        self.assertIs(caught.exception, error)
        self.assertEqual(path.read_bytes(), original)
        leftovers = list(self.temp_dir.glob(".presets.yaml.*.tmp"))
        self.assertEqual(len(leftovers), 1)
        leftovers[0].unlink()
