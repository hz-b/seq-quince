from __future__ import annotations

import asyncio
import logging
from threading import Event
from typing import Any, Dict, Mapping

from epics.ca import ChannelAccessException
from kiwi_scan.epics_wrapper import EpicsPV

from seq_quince.device.device_abs import DeviceABC, get_param
from seq_quince.device.lifecycle import (
    ensure_open,
    has_pending_work,
    run_blocking,
    track_resource,
)

logger = logging.getLogger(__name__)

class SequenceDevice(DeviceABC):
    """
    Executes EpicsPv.put() sequence defined by raw config.
    """
    _cleanup_tracks_work = True

    def __init__(self, name: str, cfg: Mapping[str, Any]) -> None:
        """
        Initialize the sequence device.

        Parameters
        ----------
        name: name of the sequence
        cfg : dict
            Configuration dictionary. Must contain at least the following key:

                sequence : dict
                    A dictionary mapping logical names (e.g., "Harmonic", "IDOn", ...) to configuration dicts.
                    Each configuration dict supports the following keys:
                        - "pv": str
                            The process variable (PV) name for channel access communication.
                        - "value": Any
                            The value to be written to the PV.
                        - "queue_delay": float, optional
                            Optional delay (in seconds) to wait after setting the value (default: implementation-defined).
                        - "desired_pv": str, optional
                            Optional second PV name for the desired value input.

            Example:
                cfg = {
                    "sequence": {
                        "Harmonic": {
                            "pv": "device:aiHarmonic",
                            "value": 3,
                            "queue_delay": 0.5,
                            "desired_pv": "device:aiHarmonicSetpoint"
                        },
                        "IDOn": {
                            "pv": "device:boIDOn",
                            "value": 1
                        }
                    }
                }
        """
        self.name = name
        self._names_cfg = cfg
        logger.debug(f"sequence: names_cfg = {self._names_cfg}")
        self._pvs: Dict[str, EpicsPV] = {}
        self._busy = False
        # Only polled/set: construction may run in a worker without a loop.
        self._abort_event = Event()
        # Pre-initialize EpicsPV objects for all defined PVs
        for pname, pconf in self._names_cfg.items():
            pvname = pconf.get("pv")
            if pvname:
                try:
                    self._pvs[pname] = EpicsPV(
                        pvname,
                        queueing_delay=pconf.get("queue_delay", 0.01)
                    )
                    track_resource(self, self._pvs[pname], self._pvs[pname].close)
                except (TypeError, ValueError, ConnectionError, ChannelAccessException) as exc:
                    logger.error("Error initializing EpicsPV for '%s': %s", pvname, exc)

    @classmethod
    def from_config_dict(cls, config: Dict[str, Any]) -> SequenceDevice:
        logger.debug(f"from_dict: config: {config}")
        name = get_param(config, "name", str, required=True)
        cfg = config.get("sequence", {})
        if name is None:
            raise ValueError("Missing required parameter: 'name'")
        return cls(name, cfg)

    async def process(self, pose: Dict[str, float]) -> None:
        """
        Sets PVs to values from config or pose. If 'desired_pv' is set,
        use that value unless overridden by pose.
        """
        ensure_open(self)
        if self.busy:
            logger.warning(f"{self.name}: SequenceDevice is already running.")
            return

        self._busy = True
        self._abort_event.clear()

        try:
            for pname, pconf in self._names_cfg.items():
                if self._abort_event.is_set():
                    logger.info(f"{self.name}: Move aborted.")
                    break

                pv = self._pvs.get(pname)
                if not pv:
                    logger.error(f"{self.name}: PV for '{pname}' not found.")
                    continue

                # Decide value to write
                value = pose.get(pname, None)
                if value is None:
                    value = await self._configured_value(pname, pconf)
                # Stop may arrive while desired-value construction/read waits.
                if self._abort_event.is_set():
                    break
                if value is None:
                    logger.warning(f"{self.name}: No value found for '{pname}'. Skipping.")
                    continue

                ok = await run_blocking(self, pv.put, value)
                if not ok:
                    logger.error(f"{self.name}: Failed to set {pv.pvname} to {value}")
                else:
                    logger.info(f"{self.name}: Set {pv.pvname} to {value}")

                # Respect queue_delay
                queue_delay = pconf.get("queue_delay", 0.01)
                await asyncio.sleep(queue_delay)

        except Exception:
            logger.exception(f"{self.name}: Exception during process()")
        finally:
            self._busy = False

    async def _configured_value(self, pname: str, pconf: Mapping[str, Any]) -> Any:
        desired_pvname = pconf.get("desired_pv")
        if not desired_pvname:
            return pconf.get("value")
        desired_pv = await run_blocking(self, self._create_desired_pv, desired_pvname)
        if self._abort_event.is_set():
            return None
        value = await run_blocking(self, desired_pv.get)
        logger.info("%s: Got desired value %s from %s for '%s'", self.name, value, desired_pvname, pname)
        return value

    def _create_desired_pv(self, pvname: str) -> EpicsPV:
        # Register inside the worker: cancellation may outlive PV construction.
        pv = EpicsPV(pvname)
        track_resource(self, pv, pv.close)
        return pv

    async def snapshot(self) -> Dict[str, float]:
        """
        Gets a snapshot of the current "pv"-values using EpicsPV.get()
        """
        ensure_open(self)
        pose = {}
        for pname, pv in self._pvs.items():
            value = await run_blocking(self, pv.get)
            pose[pname] = value
        return pose

    @property
    def busy(self) -> bool:
        return self._busy or has_pending_work(self)

    async def stop(self) -> None:
        """
        Signals to abort the running sequence ASAP.
        """
        self._abort_event.set()
        # Wait briefly for sequence to acknowledge stop
        for _ in range(20):
            if not self.busy:
                break
            await asyncio.sleep(0.1)
        if self._busy:
            logger.warning(f"{self.name}: Could not stop sequence cleanly.")


