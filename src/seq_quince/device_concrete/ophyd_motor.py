import logging
from functools import partial
from typing import Any, Dict

from ophyd import EpicsMotor

from seq_quince.device.device_abs import DeviceABC, get_param
from seq_quince.device.lifecycle import (
    MotionState,
    ensure_open,
    has_pending_work,
    run_blocking,
    track_resource,
)

logger = logging.getLogger(__name__)

class OphydMotorDevice(DeviceABC):
    _cleanup_tracks_work = True

    def __init__(self, name: str, pv_prefix: str):
        self.name = name
        # Define the motor using ophyd's EpicsMotor
        logger.info(f"Connect to motor record {pv_prefix}")
        self.motor = EpicsMotor(pv_prefix, name=name)
        track_resource(self, self.motor, self.motor.destroy)
        self._busy = False
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

        self._busy = True

        try:
            move = partial(self.motor.move, target, wait=True)
            await run_blocking(self, move)
        except BaseException:
            self._motion.watch()
            raise
        finally:
            self._busy = False
        await self._motion.refresh()

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

    @property
    def busy(self) -> bool:
        # EpicsMotor.moving forces a CA read; only workers sample that property.
        return self._busy or self._motion.busy or has_pending_work(self)

    async def stop(self) -> None:
        try:
            await run_blocking(self, self.motor.stop)
        except BaseException:
            self._motion.watch()
            raise
        finally:
            self._busy = False
        await self._motion.refresh()
