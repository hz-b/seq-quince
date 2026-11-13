from __future__ import annotations

import asyncio
from typing import Any, Dict

from seq_quince.device.device_abs import DeviceABC, get_param
from seq_quince.device.lifecycle import ensure_open


class SimDevice(DeviceABC):
    """
    A no-hardware device:

    * accepts arbitrary axis names (x, y, gap, …)
    * `process()` → sleeps `delay` seconds, then stores the targets
    * `snapshot()` → returns the stored values instantly
    * `busy` is True while the sleep is in progress
    """

    _cleanup_tracks_work = True

    def __init__(
        self,
        name: str,
        initial_pose: Dict[str, float] | None = None,
        delay: float = 0.05,
    ) -> None:
        self.name = name
        self._pose: Dict[str, float] = dict(initial_pose or {})
        self._delay = delay
        self._busy = False

    @classmethod
    def from_config_dict(cls, config: Dict[str, Any]) -> SimDevice:
        name = get_param(config, "name", str, required=True)
        delay = get_param(config, "delay", float, default=0.01)
        initial_pose = config.get("initial_pose", {"position": 0.0})
        if name is None:
            raise ValueError("Missing required parameter: 'name'")
        if delay is None:
            raise ValueError("Missing required parameter: 'delay'")
        return cls(name=name, initial_pose=initial_pose, delay=delay)

    # ------------------------------------------------------------------ ABC
    async def process(self, pose: Dict[str, float]) -> None:
        """Pretend to move—just sleep and save the values."""
        ensure_open(self)
        self._busy = True
        try:
            await asyncio.sleep(self._delay)
            self._pose.update(pose)
        finally:
            self._busy = False

    async def snapshot(self) -> Dict[str, float]:
        """Return a *copy* of the last stored pose."""
        ensure_open(self)
        return dict(self._pose)

    @property
    def busy(self) -> bool:
        return self._busy

    async def stop(self) -> None:
        """Instantly clear busy flag (nothing else to stop)."""
        self._busy = False

