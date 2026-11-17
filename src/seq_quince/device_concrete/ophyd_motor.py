import logging
from functools import partial
from typing import Any, Dict

from ophyd import EpicsMotor

from seq_quince.device.device_abs import DeviceABC, get_param
from seq_quince.device.lifecycle import (
    MotionBusyMixin,
    MotionState,
    ensure_open,
    run_blocking,
    track_resource,
)

logger = logging.getLogger(__name__)

class OphydMotorDevice(MotionBusyMixin, DeviceABC):
    _cleanup_tracks_work = True

    def __init__(self, name: str, pv_prefix: str):
        self.name = name
        # Define the motor using ophyd's EpicsMotor
        logger.info(f"Connect to motor record {pv_prefix}")
        self.motor = EpicsMotor(pv_prefix, name=name)
        track_resource(self, self.motor, self.motor.destroy)
        # EpicsMotor.moving forces a CA read; only workers sample that property.
        self._motion = MotionState(self, lambda: bool(self.motor.moving))

    @classmethod
    def from_config_dict(cls, config: Dict[str, Any]) -> "OphydMotorDevice":
        name = get_param(config, "name", str, required=True)
        pv_prefix = get_param(config, "pv_prefix", str, required=True)
        if name == None:
            raise ValueError("name is required")
        if pv_prefix == None:
            raise ValueError("pv_prefix is required")
        return cls(
            name=name,
            pv_prefix=pv_prefix)

    async def process(self, pose: Dict[str, float]) -> None:
        ensure_open(self)
        target = pose.get("position")
        if target is None:
            raise ValueError("Pose must have a 'position' key")

        await self._motion.command(partial(self.motor.move, target, wait=True))

    async def snapshot(self) -> Dict[str, float]:
        ensure_open(self)
        pos = await run_blocking(self, lambda: self.motor.position)
        if pos is None:
            raise RuntimeError(f"Motor {self.name!r} has no position readback")
        try:
            position = float(pos)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Motor {self.name!r} returned an invalid position: {pos!r}") from exc
        await self._motion.refresh()
        return {"position": position}

    async def stop(self) -> None:
        await self._motion.command(self.motor.stop)
