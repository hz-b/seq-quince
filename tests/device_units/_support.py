"""Small unittest fixtures; all device logic and kiwi-scan models stay real."""

import asyncio
import socket
import sys
import unittest
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock, patch


class UnexpectedHardwareAccess(BaseException):
    """Escape device handlers that deliberately catch ordinary Exceptions."""


class DeviceTestCase(unittest.TestCase):
    """One real loop per test, with synchronous hardware boundaries mocked."""

    def setUp(self):
        super().setUp()
        self._previous_loop = None
        if sys.version_info < (3, 10):
            try:
                self._previous_loop = asyncio.get_event_loop()
            except RuntimeError:
                pass
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.addCleanup(self._close_loop)

        def forbidden(*args, **kwargs):
            raise UnexpectedHardwareAccess(
                "Unexpected hardware access; patch the driver boundary"
            )

        boundaries = {
            "actuator": ("EpicsActuator",),
            "hexapod": ("EpicsActuator", "PV"),
            "mono_zero_order": ("EpicsPV",),
            "ophyd_motor": ("EpicsMotor",),
            "sequence": ("EpicsPV",),
            "toggle": ("EpicsPV",),
            "wait_condition": ("EpicsPV",),
        }
        for name, attributes in boundaries.items():
            module = import_module("seq_quince.device_concrete." + name)
            for attribute in attributes:
                self.patch(module, attribute, forbidden)
        self.patch(socket, "create_connection", forbidden)

    def _close_loop(self):
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
        self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        if hasattr(self.loop, "shutdown_default_executor"):
            self.loop.run_until_complete(self.loop.shutdown_default_executor())
        self.loop.close()
        asyncio.set_event_loop(self._previous_loop)

    def run_async(self, coroutine):
        """Use the same loop that bound asyncio.Event during construction on 3.8."""
        return self.loop.run_until_complete(coroutine)

    def patch(self, target, attribute, *args, **kwargs):
        patcher = patch.object(target, attribute, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def init_pvs(self):
        self.pvs = {}

        def create(name, **kwargs):
            if name not in self.pvs:
                self.pvs[name] = SimpleNamespace(
                    pvname=name, get=Mock(return_value=0.0), put=Mock(return_value=True),
                    close=Mock()
                )
            return self.pvs[name]

        self.pv_factory = Mock(side_effect=create)
        for name in ("toggle", "sequence", "wait_condition"):
            self.patch(
                import_module("seq_quince.device_concrete." + name),
                "EpicsPV",
                self.pv_factory,
            )

    def init_actuators(self):
        self.drivers = []

        def create(config):
            driver = SimpleNamespace(
                rbv=0.0,
                run_move=Mock(return_value=None),
                is_ready=Mock(return_value=True),
                stop=Mock(return_value=None),
                close=Mock(),
            )
            self.drivers.append(driver)
            return driver

        self.driver_factory = Mock(side_effect=create)
        for name in ("actuator", "hexapod"):
            self.patch(
                import_module("seq_quince.device_concrete." + name),
                "EpicsActuator",
                self.driver_factory,
            )
