from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

from kiwi_scan.actuator_concrete.single_epics import EpicsActuator
from kiwi_scan.datamodels import ActuatorConfig

from seq_quince.device.device_abs import DeviceABC, get_param
from seq_quince.device.lifecycle import (
    MotionBusyMixin,
    MotionState,
    ensure_open,
    run_blocking,
    track_resource,
)

logger = logging.getLogger(__name__)

class ActuatorDevice(MotionBusyMixin, DeviceABC):
    _cleanup_tracks_work = True

    def __init__(self, name: str, cfg: Mapping[str, Any]) -> None:
        """
        name: Human-readable identifier (“mono.theta”).
        cfg: Raw YAML block (`dict[str, Any]`) for this actuator.
             It is validated and converted to an `ActuatorConfig` instance.
        """
        self.name: str = name
        self.cfg: ActuatorConfig = ActuatorConfig.from_dict(dict(cfg))

        # build the Actuator driver using the dataclass
            # instantiate the Actuator
        self._actuator = EpicsActuator(self.cfg)
        track_resource(self, self._actuator, self._actuator.close)
        self._motion = MotionState(self, lambda: not self._actuator.is_ready())

    @classmethod
    def from_config_dict(cls, config: Dict[str, Any]) -> ActuatorDevice:
        name = get_param(config, "name", str, required=True)
        if not isinstance(name, str):
            raise TypeError("'name' must be a string")

        actuator_config = config.get("config")
        if not isinstance(actuator_config, Mapping):
            raise TypeError("'config' must be a mapping")

        logger.debug("actuator_config: %s", actuator_config)
        return cls(name, actuator_config)

    async def process(self, pose: Dict[str, float]) -> None:
        ensure_open(self)
        position = pose.get("position")
        if position is None:
            raise ValueError("Pose must have a 'position' key")
        logger.info(f"actuator position: {position}")
        await self._motion.command(self._actuator.run_move, position, True)

    async def snapshot(self) -> Dict[str, float]:
        ensure_open(self)
        position = await run_blocking(self, lambda: self._actuator.rbv)
        if position is None:
            raise RuntimeError(f"Cannot snapshot actuator {self.name!r}: readback unavailable")
        await self._motion.refresh()
        return {"position": float(position)}

    async def stop(self) -> None:
        """Await the driver's stop request; this does not confirm motion ceased."""
        await self._motion.command(self._actuator.stop)

    def get_capability(self, name: str) -> Optional[Any]:
        if name == "actuator":
            return self._actuator
        return super().get_capability(name)
