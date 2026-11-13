from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping

from seq_quince.device.device_abs import DeviceABC


class SequenceError(RuntimeError):
    """Raised if the builder detects an impossible or ill-formed request."""

@dataclass
class MoveStep:
    """
    A single, atomic motion request: drive *one* Device to *one* pose.

    `group_id` lets the scheduler know which steps can run in parallel.
    Steps with the same `group_id` are awaited together; `None` means
    “run immediately, then wait (barrier) before continuing”.
    """
    device: DeviceABC
    pose: Dict[str, float]
    group_id: int | None = None
    description: str = field(default_factory=str)

    # debug output
    def __repr__(self) -> str:
        g = f"group={self.group_id}" if self.group_id is not None else "barrier"
        return f"<MoveStep {self.device.name}→{self.pose} {g}>"


def build_sequence(
    devices: Mapping[str, DeviceABC],
    target: Mapping[str, Mapping[str, float]],
    *,
    default_grouping: str = "parallel",    # "parallel" | "serial"
) -> List[MoveStep]:
    """
    Sequence builder from YAML structure

    Convert `{device_name: {axis: value, …}}` into a flat list of MoveStep.

    Simple rule for now:
    * if `default_grouping == "parallel"` – every device goes in group 0
      (true parallel).  We insert a *barrier* (group=None) at the end so
      subsequent operations can safely assume all moves are finished.
    * if `serial` – devices go in successive groups 0,1,2,… (one at a time).
    More sophisticated YAML flags (e.g. `group:` keys) can be added later.
    """
    if default_grouping not in {"parallel", "serial"}:
        raise ValueError("default_grouping must be 'parallel' or 'serial'")

    steps: list[MoveStep] = []

    if default_grouping == "parallel":
        # All devices share the same group id (0) (executed together)
        for name, pose in target.items():
            try:
                dev = devices[name]
            except KeyError as exc:
                raise SequenceError(f"Unknown device '{name}'") from exc
            steps.append(MoveStep(dev, dict(pose), group_id=0, description=name))
        # Final barrier
        steps.append(MoveStep(device=_BarrierDevice(), pose={}, group_id=None,
                              description="BARRIER"))
        return steps

    # --- serial -------------------------------------------------------------
    # assigning each device a different group ID allows these steps to run sequentially
    for grp_id, (name, pose) in enumerate(target.items()):
        if name not in devices:
            raise SequenceError(f"Unknown device '{name}'")
        steps.append(MoveStep(devices[name], dict(pose), group_id=grp_id,
                              description=name))
    # implicit barrier unnecessary, serial groups already block
    return steps


class _BarrierDevice(DeviceABC):
    """ Internal helper - a dummy device to mark barriers """

    @classmethod
    def from_config_dict(cls, config: Dict[str, Any]) -> _BarrierDevice:
        return cls()

    """Placeholder that does nothing, used to encode explicit barriers."""
    name = "__barrier__"

    async def process(self, pose):
        return

    async def snapshot(self):
        return {}

    @property
    def busy(self):
        return False

    async def stop(self):
        return

