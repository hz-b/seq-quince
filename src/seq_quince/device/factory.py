from threading import RLock
from typing import Any, Dict, List, Type

from seq_quince.device.device_abs import DeviceABC
from seq_quince.device.extensions import DeviceExtensionLoader
from seq_quince.device.lifecycle import own_device
from seq_quince.device_concrete.actuator import ActuatorDevice
from seq_quince.device_concrete.hexapod import HexapodDevice
from seq_quince.device_concrete.mono_zero_order import MonoZeroOrderDevice
from seq_quince.device_concrete.ophyd_motor import OphydMotorDevice
from seq_quince.device_concrete.scan import ScanDevice
from seq_quince.device_concrete.sequence import SequenceDevice
from seq_quince.device_concrete.sim import SimDevice
from seq_quince.device_concrete.toggle import ToggleDevice
from seq_quince.device_concrete.wait_condition import WaitConditionDevice

DEVICE_REGISTRY: Dict[str, Type[DeviceABC]] = {
    "OphydMotorDevice": OphydMotorDevice,
    "ScanDevice": ScanDevice,
    "ToggleDevice": ToggleDevice,
    "SequenceDevice": SequenceDevice,
    "ActuatorDevice": ActuatorDevice,
    "HexapodDevice": HexapodDevice,
    "SimDevice": SimDevice,
    "MonoZeroOrderDevice": MonoZeroOrderDevice,
    "WaitConditionDevice": WaitConditionDevice,
}

_extension_loader = DeviceExtensionLoader()
_registry_lock = RLock()

# ---- Factory Class ---------------------------------------------------------

class DeviceFactory:
    @classmethod
    def load_extensions(cls) -> None:
        """
        Register devices exported by new SEQ_QUINCE_DEVICE_PATH directories.

        Discovery happens automatically during creation and type listing. This
        method also lets callers check extensions before creating any devices.
        """
        with _registry_lock:
            DEVICE_REGISTRY.update(_extension_loader.load(DEVICE_REGISTRY))

    @classmethod
    def register(cls, name: str, device_cls: Type[DeviceABC]) -> None:
        # Preserve the existing explicit-registration replacement behavior.
        with _registry_lock:
            DEVICE_REGISTRY[name] = device_cls

    @classmethod
    def create(cls, config: Dict[str, Any]) -> DeviceABC:
        """Create a device from config dict. Must include 'type' key."""
        device_type = config.get("type")
        if not device_type:
            raise ValueError("Device config missing required key: 'type'")

        with _registry_lock:
            cls.load_extensions()
            device_cls = DEVICE_REGISTRY.get(device_type)
            if not device_cls:
                raise ValueError(f"Unknown device type '{device_type}'. Available: {list(DEVICE_REGISTRY.keys())}")

        # Use the device's own config loader
        device = device_cls.from_config_dict(config)
        own_device(device)
        return device

    @classmethod
    def available_types(cls) -> List[str]:
        with _registry_lock:
            cls.load_extensions()
            return list(DEVICE_REGISTRY.keys())

