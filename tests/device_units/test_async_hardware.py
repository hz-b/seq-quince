"""Blocking driver calls must yield and remain owned after waiter cancellation."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from seq_quince.device_concrete import (
    actuator,
    ophyd_motor,
    scan,
    sequence,
    toggle,
    wait_condition,
)

from ._support import DeviceTestCase


class AsyncHardwareTests(DeviceTestCase):
    def setUp(self):
        super().setUp()
        self.init_pvs()

    async def assert_worker_yields(self, operation, install, *, result=None):
        """Hold a hardware boundary until another loop task releases it."""
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        entered = asyncio.Event()
        release = threading.Event()

        def blocked(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(2):
                raise TimeoutError("hardware test worker was not released")
            return result

        install(blocked)
        task = asyncio.create_task(operation())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            release.set()
            await asyncio.wait_for(task, 1)

    def test_sequence_desired_construction_reads_writes_and_snapshot_yield(self):
        for boundary in ("construct", "desired_get", "put", "snapshot"):
            with self.subTest(boundary=boundary):
                device = sequence.SequenceDevice("seq", {
                    "target": {"pv": "TEST:SET", "desired_pv": "TEST:DESIRED", "queue_delay": 0},
                })
                desired = self.pv_factory("TEST:DESIRED")
                factory = self.pv_factory

                def install(blocked, boundary=boundary, desired=desired, factory=factory):
                    if boundary == "construct":
                        def create(name, **kwargs):
                            blocked()
                            return factory(name, **kwargs)

                        self.patch(sequence, "EpicsPV", create)
                    elif boundary == "desired_get":
                        desired.get.side_effect = blocked
                    else:
                        pv = self.pvs["TEST:SET"]
                        getattr(pv, "get" if boundary == "snapshot" else "put").side_effect = blocked

                async def exercise(device=device, boundary=boundary, install=install):
                    operation = device.snapshot if boundary == "snapshot" else lambda: device.process({})
                    await self.assert_worker_yields(operation, install, result=2)
                    await device.close()

                self.run_async(exercise())
                # Restore driver mocks between subtests that share the PV fixture.
                self.pvs.clear()
                self.patch(sequence, "EpicsPV", self.pv_factory)

    def test_wait_snapshot_constructs_pv_in_worker(self):
        device = wait_condition.WaitConditionDevice("wait", pv="TEST:READY", value=1)
        factory = self.pv_factory

        def install(blocked):
            def create(name, **kwargs):
                blocked()
                return factory(name, **kwargs)

            self.patch(wait_condition, "EpicsPV", create)

        async def exercise():
            await self.assert_worker_yields(device.snapshot, install)
            await device.close()

        self.run_async(exercise())

    def test_cancelled_sequence_construction_keeps_late_pv_owned(self):
        device = sequence.SequenceDevice("seq", {
            "target": {"pv": "TEST:SET", "desired_pv": "TEST:LATE", "queue_delay": 0},
        })
        factory = self.pv_factory
        release = threading.Event()

        async def exercise():
            loop = asyncio.get_running_loop()
            entered = asyncio.Event()

            def create(name, **kwargs):
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(2):
                    raise TimeoutError("construction worker was not released")
                return factory(name, **kwargs)

            self.patch(sequence, "EpicsPV", create)
            operation = asyncio.create_task(device.process({}))
            closing = None
            try:
                await asyncio.wait_for(entered.wait(), 1)
                operation.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await operation
                self.assertTrue(device.busy)
                closing = asyncio.create_task(device.close())
                await asyncio.sleep(0)
                self.assertFalse(closing.done())
                self.pvs["TEST:SET"].close.assert_not_called()
            finally:
                release.set()
                if closing is not None:
                    await asyncio.wait_for(closing, 1)
                await asyncio.gather(operation, return_exceptions=True)

        self.run_async(exercise())
        self.pvs["TEST:SET"].put.assert_not_called()
        self.pvs["TEST:LATE"].get.assert_not_called()
        for pv in self.pvs.values():
            pv.close.assert_called_once_with()

    def test_cancelled_toggle_read_or_write_stays_busy_until_worker_settles(self):
        for boundary in ("get", "put"):
            with self.subTest(boundary=boundary):
                self.pvs.clear()
                device = toggle.ToggleDevice("toggle", "TEST:RBV", 1, "TEST:CMD")
                release = threading.Event()

                async def exercise(device=device, boundary=boundary, release=release):
                    loop = asyncio.get_running_loop()
                    entered = asyncio.Event()

                    def block(*args):
                        loop.call_soon_threadsafe(entered.set)
                        if not release.wait(2):
                            raise TimeoutError("toggle worker was not released")
                        return 0 if boundary == "get" else True

                    pv = self.pvs["TEST:RBV" if boundary == "get" else "TEST:CMD"]
                    getattr(pv, boundary).side_effect = block
                    operation = asyncio.create_task(device.process({}))
                    closing = None
                    try:
                        await asyncio.wait_for(entered.wait(), 1)
                        operation.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await operation
                        await device.stop()
                        self.assertTrue(device.busy)
                        closing = asyncio.create_task(device.close())
                        await asyncio.sleep(0)
                        self.assertFalse(closing.done())
                        for owned in self.pvs.values():
                            owned.close.assert_not_called()
                    finally:
                        release.set()
                        if closing is not None:
                            await asyncio.wait_for(closing, 1)
                        await asyncio.gather(operation, return_exceptions=True)

                self.run_async(exercise())
                if boundary == "get":
                    self.pvs["TEST:CMD"].put.assert_not_called()
                for pv in self.pvs.values():
                    pv.close.assert_called_once_with()

    def test_scan_stop_yields_waits_and_propagates_failure(self):
        self.patch(scan, "get_scan_config_dir", lambda: "/test/config")
        self.patch(scan, "load_scan_config_from_file", Mock(return_value={
            "actuators": {"energy": {"pv": "TEST:ENERGY"}},
        }))
        instance = SimpleNamespace(execute=Mock(), busy=False, stop=Mock(), close=Mock())
        self.patch(scan, "create_scan_with_config", Mock(return_value=instance))
        device = scan.ScanDevice("scan", "linear", "test.yaml")

        async def exercise():
            await self.assert_worker_yields(
                device.stop, lambda blocked: setattr(instance.stop, "side_effect", blocked)
            )
            failure = OSError("scan stop failed")
            instance.stop.side_effect = failure
            with self.assertRaises(OSError) as caught:
                await device.stop()
            self.assertIs(caught.exception, failure)
            await device.close()

        self.run_async(exercise())

    def test_cancelled_scan_stop_keeps_scan_alive_until_driver_returns(self):
        self.patch(scan, "get_scan_config_dir", lambda: "/test/config")
        self.patch(scan, "load_scan_config_from_file", Mock(return_value={
            "actuators": {"energy": {"pv": "TEST:ENERGY"}},
        }))
        instance = SimpleNamespace(execute=Mock(), busy=True, stop=Mock(), close=Mock())
        self.patch(scan, "create_scan_with_config", Mock(return_value=instance))
        device = scan.ScanDevice("scan", "linear", "test.yaml")
        release = threading.Event()

        async def exercise():
            loop = asyncio.get_running_loop()
            entered = asyncio.Event()

            def stop():
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(2):
                    raise TimeoutError("scan stop worker was not released")

            instance.stop.side_effect = stop
            operation = asyncio.create_task(device.stop())
            closing = None
            try:
                await asyncio.wait_for(entered.wait(), 1)
                operation.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await operation
                closing = asyncio.create_task(device.close())
                await asyncio.sleep(0)
                self.assertFalse(closing.done())
                instance.close.assert_not_called()
            finally:
                release.set()
                if closing is not None:
                    await asyncio.wait_for(closing, 1)
                await asyncio.gather(operation, return_exceptions=True)

        self.run_async(exercise())
        instance.stop.assert_called_once_with()
        instance.close.assert_called_once_with()

    def test_stop_during_pending_read_prevents_following_write(self):
        for kind in ("sequence", "toggle"):
            with self.subTest(kind=kind):
                self.pvs.clear()
                if kind == "sequence":
                    device = sequence.SequenceDevice("seq", {
                        "target": {"pv": "TEST:SET", "desired_pv": "TEST:DESIRED", "queue_delay": 0},
                    })
                    read = self.pv_factory("TEST:DESIRED").get
                    put = self.pvs["TEST:SET"].put
                else:
                    device = toggle.ToggleDevice("toggle", "TEST:RBV", 1, "TEST:CMD")
                    read = self.pvs["TEST:RBV"].get
                    put = self.pvs["TEST:CMD"].put
                release = threading.Event()

                async def exercise(device=device, read=read, release=release, kind=kind):
                    loop = asyncio.get_running_loop()
                    entered = asyncio.Event()

                    def get():
                        loop.call_soon_threadsafe(entered.set)
                        if not release.wait(2):
                            raise TimeoutError("read worker was not released")
                        return 0

                    read.side_effect = get
                    operation = asyncio.create_task(device.process({}))
                    stopping = None
                    try:
                        await asyncio.wait_for(entered.wait(), 1)
                        stopping = asyncio.create_task(device.stop())
                        await asyncio.sleep(0)
                        requested = device._abort_event.is_set() if kind == "sequence" else device._stop_requested
                        self.assertTrue(requested)
                        self.assertFalse(operation.done())
                    finally:
                        release.set()
                        await asyncio.wait_for(operation, 1)
                        if stopping is not None:
                            await asyncio.wait_for(stopping, 1)
                        await device.close()

                self.run_async(exercise())
                put.assert_not_called()

    def test_stop_during_scan_replacement_prevents_new_execution(self):
        self.patch(scan, "get_scan_config_dir", lambda: "/test/config")
        self.patch(scan, "load_scan_config_from_file", Mock(return_value={
            "actuators": {"energy": {"pv": "TEST:ENERGY"}},
        }))
        previous = SimpleNamespace(execute=Mock(), busy=False, stop=Mock(), close=Mock())
        replacement = SimpleNamespace(execute=Mock(), busy=False, stop=Mock(), close=Mock())
        factory = self.patch(scan, "create_scan_with_config", Mock(return_value=previous))
        device = scan.ScanDevice("scan", "linear", "test.yaml")
        release = threading.Event()

        async def exercise():
            loop = asyncio.get_running_loop()
            entered = asyncio.Event()

            def create(*args):
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(2):
                    raise TimeoutError("scan replacement was not released")
                return replacement

            factory.side_effect = create
            operation = asyncio.create_task(device.process({"start": 1}))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                await asyncio.wait_for(device.stop(), 1)
                previous.stop.assert_called_once_with()
                self.assertFalse(operation.done())
            finally:
                release.set()
                await asyncio.wait_for(operation, 1)
                await device.close()

        self.run_async(exercise())
        self.assertIs(device.get_capability("scan"), previous)
        previous.execute.assert_not_called()
        replacement.execute.assert_not_called()
        previous.close.assert_called_once_with()
        replacement.close.assert_called_once_with()

    def test_busy_uses_cache_while_readiness_blocks_in_worker(self):
        for kind in ("actuator", "ophyd"):
            with self.subTest(kind=kind):
                if kind == "actuator":
                    self.init_actuators()
                    device = actuator.ActuatorDevice("axis", {"pv": "TEST:AXIS"})
                    checker = self.drivers[-1].is_ready
                    result = True
                else:
                    checker = Mock()

                    class Motor:
                        position = 0
                        destroy = Mock()

                        def __init__(self, reader):
                            self.reader = reader

                        @property
                        def moving(self):
                            return self.reader()

                    self.patch(ophyd_motor, "EpicsMotor", Mock(return_value=Motor(checker)))
                    device = ophyd_motor.OphydMotorDevice("motor", "TEST:MOTOR")
                    result = False
                release = threading.Event()

                async def exercise(device=device, checker=checker, result=result, release=release):
                    loop = asyncio.get_running_loop()
                    loop_thread = threading.get_ident()
                    entered = asyncio.Event()

                    def ready():
                        self.assertNotEqual(threading.get_ident(), loop_thread)
                        loop.call_soon_threadsafe(entered.set)
                        if not release.wait(2):
                            raise TimeoutError("readiness worker was not released")
                        return result

                    checker.side_effect = ready
                    operation = asyncio.create_task(device.snapshot())
                    try:
                        await asyncio.wait_for(entered.wait(), 1)
                        for _ in range(3):
                            self.assertTrue(device.busy)
                        self.assertEqual(checker.call_count, 1)
                    finally:
                        release.set()
                        await asyncio.wait_for(operation, 1)
                    checker.side_effect = AssertionError("busy must not read hardware")
                    self.assertFalse(device.busy)
                    self.assertEqual(checker.call_count, 1)
                    await device.close()

                self.run_async(exercise())
