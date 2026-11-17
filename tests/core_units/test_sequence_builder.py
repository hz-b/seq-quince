"""Sequence construction preserves device identity, order and independent poses."""

from types import MappingProxyType, SimpleNamespace

from seq_quince.sequence import MoveStep, SequenceError, _BarrierDevice, build_sequence

from ._support import CoreTestCase


class SequenceBuilderTests(CoreTestCase):
    def setUp(self):
        super().setUp()
        self.devices = {name: SimpleNamespace(name=name) for name in ("x", "y")}

    def test_parallel_steps_copy_targets_and_append_barrier(self):
        target = {"y": {"position": 2.0}, "x": {"position": 1.0}}
        steps = build_sequence(MappingProxyType(self.devices), target)
        self.assertEqual([step.description for step in steps], ["y", "x", "BARRIER"])
        self.assertEqual([step.group_id for step in steps], [0, 0, None])
        self.assertIs(steps[0].device, self.devices["y"])
        self.assertIs(steps[1].device, self.devices["x"])
        self.assertEqual(steps[0].pose, {"position": 2.0})
        target["y"]["position"] = 99
        self.assertEqual(steps[0].pose, {"position": 2.0})
        steps[1].pose["position"] = -1
        self.assertEqual(target["x"], {"position": 1.0})
        self.assertIsInstance(steps[-1].device, _BarrierDevice)
        self.assertEqual(steps[-1].pose, {})

    def test_serial_groups_follow_input_order_without_barrier(self):
        target = {"y": {"position": 2}, "x": {"position": 1}}
        steps = build_sequence(self.devices, target, default_grouping="serial")
        self.assertEqual(
            [(s.device.name, s.pose, s.group_id) for s in steps],
            [
                ("y", {"position": 2}, 0),
                ("x", {"position": 1}, 1),
            ],
        )
        target["x"]["position"] = 99
        self.assertEqual(steps[1].pose, {"position": 1})

    def test_empty_targets_and_invalid_grouping(self):
        self.assertEqual(build_sequence({}, {}, default_grouping="serial"), [])
        parallel = build_sequence({}, {})
        self.assertEqual(len(parallel), 1)
        self.assertIsNone(parallel[0].group_id)
        with self.assertRaisesRegex(ValueError, "parallel.*serial"):
            build_sequence({}, {}, default_grouping="invalid")

    def test_unknown_devices_are_rejected_in_each_mode(self):
        for grouping in ("parallel", "serial"):
            with self.subTest(grouping=grouping):
                with self.assertRaisesRegex(
                    SequenceError, "Unknown device 'missing'"
                ) as caught:
                    build_sequence(
                        self.devices,
                        {"missing": {"position": 1}},
                        default_grouping=grouping,
                    )
                if grouping == "parallel":
                    self.assertIsInstance(caught.exception.__cause__, KeyError)

    def test_rollback_is_off_by_default_and_opted_in_per_entry(self):
        target = {"y": {"position": 2, "rollback": True}, "x": {"position": 1}}
        for grouping in ("parallel", "serial"):
            with self.subTest(grouping=grouping):
                steps = build_sequence(self.devices, target, default_grouping=grouping)
                by_name = {s.description: s for s in steps if s.group_id is not None}
                self.assertTrue(by_name["y"].rollback)
                self.assertFalse(by_name["x"].rollback)
                # The option is not part of the pose passed to process().
                self.assertEqual(by_name["y"].pose, {"position": 2})
                if grouping == "parallel":
                    self.assertFalse(steps[-1].rollback)  # barrier
        self.assertEqual(target["y"], {"position": 2, "rollback": True})

    def test_rollback_false_is_accepted_and_stripped(self):
        steps = build_sequence(
            self.devices, {"x": {"position": 1, "rollback": False}}, default_grouping="serial"
        )
        self.assertFalse(steps[0].rollback)
        self.assertEqual(steps[0].pose, {"position": 1})

    def test_non_boolean_rollback_is_rejected(self):
        for value in ("yes", 1, None):
            for grouping in ("parallel", "serial"):
                with self.subTest(value=value, grouping=grouping), self.assertRaisesRegex(
                    SequenceError, "'rollback' for device 'x'"
                ):
                    build_sequence(
                        self.devices,
                        {"x": {"position": 1, "rollback": value}},
                        default_grouping=grouping,
                    )

    def test_move_step_description_and_repr(self):
        step = MoveStep(self.devices["x"], {"position": 1}, group_id=3)
        self.assertEqual(step.description, "")
        self.assertIn("x", repr(step))
        self.assertIn("group=3", repr(step))
        self.assertIn("{'position': 1}", repr(step))
        self.assertIn("barrier", repr(MoveStep(self.devices["x"], {})))
        self.assertNotIn("rollback", repr(step))
        self.assertIn("rollback", repr(MoveStep(self.devices["x"], {}, 0, rollback=True)))

    def test_barrier_implements_no_op_device_contract(self):
        barrier = _BarrierDevice.from_config_dict({})
        self.assertEqual(barrier.name, "__barrier__")
        self.assertFalse(barrier.busy)
        self.assertIsNone(self.run_async(barrier.process({"ignored": 1})))
        self.assertEqual(self.run_async(barrier.snapshot()), {})
        self.assertIsNone(self.run_async(barrier.stop()))
