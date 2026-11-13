from __future__ import annotations

import logging
from typing import Any, Dict

from kiwi_scan.epics_wrapper import EpicsPV

from seq_quince.device.device_abs import DeviceABC, get_param
from seq_quince.device.lifecycle import (
    ensure_open,
    has_pending_work,
    run_blocking,
    track_resource,
)

logger = logging.getLogger(__name__)

class ToggleDevice(DeviceABC):
    """
    Device that toggles a PV if its readback value is not at the setpoint.
    """
    _cleanup_tracks_work = True


    def __init__(
        self,
        name: str,
        readback_pv: str,
        setpoint: float,
        toggle_pv: str,
        toggle_val: float = 1.0
    ):

        self.name = name
        logger.info(f"readback_pv: {readback_pv}")
        self.readback_pv = EpicsPV(readback_pv)
        track_resource(self, self.readback_pv, self.readback_pv.close)
        #self.readback_pv.check_pv()
        self.setpoint = setpoint
        self.toggle_pv = EpicsPV(toggle_pv)
        track_resource(self, self.toggle_pv, self.toggle_pv.close)
        #self.toggle_pv.check_pv()
        self.toggle_val = toggle_val
        self._busy = False
        self._stop_requested = False

    @classmethod
    def from_config_dict(cls, config: Dict[str, Any]) -> ToggleDevice:
        logger.debug(f"from_dict: config: {config}")
        name = get_param(config, "name", str, required=True)
        readback_pv = get_param(config, "readback_pv", str, required=True)
        setpoint = get_param(config, "setpoint", float, required=True)
        toggle_pv = get_param(config, "toggle_pv", str, required=True)
        toggle_val = get_param(config, "toggle_val", float, default=1.0)

        logger.info(f"from_dict: readback_pv: {readback_pv}")
        logger.info(f"from_dict: name: {name}")
        if name is None:
            raise ValueError("Missing required parameter: 'name'")
        if readback_pv is None:
            raise ValueError("Missing required parameter: 'readback_pv'")
        if setpoint is None:
            raise ValueError("Missing required parameter: 'setpoint'")
        if toggle_pv is None:
            raise ValueError("Missing required parameter: 'toggle_pv'")
        if toggle_val is None:
            raise ValueError("Missing required parameter: 'toggle_val'")
        return cls(
            name = name,
            readback_pv = readback_pv,
            setpoint = setpoint,
            toggle_pv = toggle_pv,
            toggle_val = toggle_val)

    async def process(self, pose: Dict[str, float]) -> None:
        """
        If the readback value is not at setpoint, toggle the PV.
        pose: {"setpoint": 1}
        """
        ensure_open(self)
        self._busy = True
        self._stop_requested = False
        try:
            # Setpoint may be overridden by pose
            setpoint = pose.get("setpoint", self.setpoint)
            readback = await run_blocking(self, self.readback_pv.get)
            if self._stop_requested:
                return
            # print(f"[TOGGLE] readback: {readback}, setpoint {setpoint}")
            if readback != setpoint:
                logger.info(f"[{self.name}] Readback={readback} != setpoint={setpoint}, toggling...")
                ok = await run_blocking(self, self.toggle_pv.put, self.toggle_val)
                if not ok:
                    logger.error(f"[{self.name}] Failed to toggle PV {self.toggle_pv.pvname} to {self.toggle_val}")
            else:
                logger.debug(f"[{self.name}] Already at setpoint {setpoint}, nothing to do.")
        except Exception:
            logger.exception(f"[{self.name}] Exception in process().")
        finally:
            self._busy = False

    async def snapshot(self) -> Dict[str, float]:
        """
        Return {"setpoint": current readback value} for replay by process().
        """
        ensure_open(self)
        try:
            value = await run_blocking(self, self.readback_pv.get)
        except Exception:
            logger.exception(f"[{self.name}] Exception in snapshot().")
            value = None
        if value == None:
            raise RuntimeError("Could not get value")
        return {"setpoint": value}

    @property
    def busy(self) -> bool:
        return self._busy or has_pending_work(self)

    async def stop(self) -> None:
        # An in-flight put cannot be recalled; suppress a put after a pending read.
        self._stop_requested = True
        self._busy = False

