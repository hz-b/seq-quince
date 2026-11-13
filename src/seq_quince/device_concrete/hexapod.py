import asyncio
import logging
import math
from typing import Any, Dict, Mapping, Optional

from epics import PV
from kiwi_scan.actuator_concrete.single_epics import EpicsActuator
from kiwi_scan.datamodels import ActuatorConfig

from seq_quince.device.device_abs import DeviceABC, get_param
from seq_quince.device.lifecycle import (
    MotionState,
    ensure_open,
    has_pending_work,
    run_blocking,
    track_resource,
)

logger = logging.getLogger(__name__)

class HexapodDevice(DeviceABC):
    """
    Concrete DeviceABC for a 6‐degree‐of‐freedom hexapod.  Supports an optional
    “coordinated move” feature: if enabled, it writes a user‐specified value to
    a “run after” MBBO PV just prior to dispatching the six actuator moves.
    """
    _cleanup_tracks_work = True
    _axis_order = ("Tx", "Ty", "Tz", "Rx", "Ry", "Rz")


    def __init__(self, name: str, cfg: Mapping[str, Any]) -> None:
        """
        name:   Human‐readable identifier ("mono.hexapod").
        cfg:    A dict with at least these keys:
                   • "axes": a dict mapping each of [ "Tx", "Ty", "Tz", "Rx", "Ry", "Rz" ]
                     to the nested ActuatorConfig (data model from scan) dict for that axis.
                   • (optional) "enable_coordinated": bool
                   • (if enable_coordinated) "run_after_pv": str     # e.g. "HEX1OS12L:hexapod:mbboRunAfterValue"
                   • (if enable_coordinated) "run_after_value": int   # e.g. 1 to enable
        """
        self.name = name
        self._stop_requested = False

        # --- Parse coordinated‐move settings --------------------------------
        self._enable_coordinated = bool(cfg.get("enable_coordinated", False))
        self._run_after_pv_name = cfg.get("run_after_pv", None)
        self._run_after_value = cfg.get("run_after_value", None)
        self._run_after_pv = None
        if self._run_after_pv_name:
            # Create a pyepics PV object for writing the MBBO run‐after field
            self._run_after_pv = PV(self._run_after_pv_name)
            if self._run_after_pv:
                track_resource(self, self._run_after_pv, self._run_after_pv.disconnect)

        if self._run_after_pv_name:
            if not self._run_after_pv:
                raise ValueError(f"PV not created: {self._run_after_pv_name!r}")
            if self._run_after_value is None:
                raise ValueError(f"PV created but no value defined: {self._run_after_value!r}")

        # --- Build six Actuator instances ------------------------------------
        axes_cfg: dict = cfg.get("axes", {})
        required_axes = self._axis_order
        missing = [ax for ax in required_axes if ax not in axes_cfg]
        if missing:
            raise ValueError(f"Missing actuator‐config for axes: {missing!r}")

        self._actuators: Dict[str, EpicsActuator] = {}
        for axis in required_axes:
            acfg = ActuatorConfig.from_dict(axes_cfg[axis])
            self._actuators[axis] = EpicsActuator(acfg)
            track_resource(self, self._actuators[axis], self._actuators[axis].close)
        self._motion = MotionState(self, self._read_busy)

    @classmethod
    def from_config_dict(cls, config: Dict[str, Any]) -> "HexapodDevice":
        name = get_param(config, "name", str, required=True)
        if name is None:
            raise ValueError("Missing required parameter: 'name'")

        hexapod_config = config.get("config")
        if not isinstance(hexapod_config, Mapping):
            raise TypeError("Parameter 'config' must be a mapping")

        logger.debug("hexapod_config: %s", hexapod_config)
        return cls(name, hexapod_config)

    async def process(self, pose: Dict[str, float]) -> None:
        """
        Drive one axis, or restore all six axes sequentially when uncoordinated.
        Coordinated mode dispatches all six axes together.
        Write the configured MBBO "run after" PV before issuing the sub-moves.
        Targets use engineering units (µm for translations, arcsec for rotations).
        """
        ensure_open(self)
        self._validate_pose(pose)

        self._stop_requested = False
        try:
            if self._run_after_pv:
                await run_blocking(self, self._run_after_pv.put, self._run_after_value)
            self._check_stop_requested()

            if self._enable_coordinated:
                await asyncio.gather(*(
                    run_blocking(self, self._actuators[axis].run_move, target, True)
                    for axis, target in pose.items()
                ))
            else:
                # Await each completed move before dispatching the next one.
                # Saved snapshot key order never changes restoration order.
                for axis in self._axis_order:
                    if axis in pose:
                        await run_blocking(self, self._actuators[axis].run_move, pose[axis], True)
                        self._check_stop_requested()
            await self._motion.refresh()
        except BaseException:
            self._motion.watch()
            raise

    def _validate_pose(self, pose: Dict[str, float]) -> None:
        # --- Validate the incoming pose dict -------------------------------
        required_axes = set(self._actuators.keys())
        provided_axes = set(pose.keys())
        if self._enable_coordinated:
            if provided_axes != required_axes:
                missing = required_axes - provided_axes
                extra = provided_axes - required_axes
                msgs = []
                if missing:
                    msgs.append(f"missing coordinates: {sorted(missing)}")
                if extra:
                    msgs.append(f"unexpected keys: {sorted(extra)}")
                raise ValueError("Pose dict must contain exactly Tx, Ty, Tz, Rx, Ry, Rz. " +
                                 "; ".join(msgs))
        else:
            # Full snapshots are replayable without changing the IOC's mode.
            unknown = provided_axes - required_axes
            if unknown:
                raise ValueError(f"Unexpected keys in pose: {sorted(unknown)}. "
                                 f"Allowed: {sorted(required_axes)}.")
            if len(provided_axes) != 1 and provided_axes != required_axes:
                raise ValueError(f"Pose dict must contain exactly one axis key or all six axes "
                                 f"(got {sorted(provided_axes)}), allowed: {sorted(required_axes)}.")

        # Validate every target before the run-after put or any axis dispatch.
        # A bad later target must not leave a snapshot restoration half-issued.
        for axis, target in pose.items():
            try:
                finite = math.isfinite(target)
            except (TypeError, ValueError, OverflowError):
                finite = False
            if not finite:
                raise ValueError(f"Target for axis '{axis}' must be a finite number")

    def _check_stop_requested(self) -> None:
        if self._stop_requested:
            raise RuntimeError(f"Hexapod '{self.name}' movement stopped")

    async def snapshot(self) -> Dict[str, float]:
        """
        Return the current read‐back of all six axes as
        { "Tx": float, "Ty": float, "Tz": float, "Rx": float, "Ry": float, "Rz": float }.
        """
        ensure_open(self)
        result: Dict[str, float] = {}
        for axis_name, actuator in self._actuators.items():
            value = await run_blocking(self, lambda act=actuator: act.rbv)
            if value is None:
                raise RuntimeError(f"No read-back value available for axis '{axis_name}'")
            result[axis_name] = float(value)
        await self._motion.refresh()
        return result

    def _read_busy(self) -> bool:
        # is_ready() can make a Channel Access fallback read, even with monitors.
        return any(not act.is_ready() for act in self._actuators.values())

    @property
    def busy(self) -> bool:
        """
        Cached motion/readiness state, including outstanding executor work.
        """
        return self._motion.busy or has_pending_work(self)

    async def stop(self) -> None:
        """
        Best‐effort immediate stop of all six actuators.  Leaves each in a recoverable state.
        """
        self._stop_requested = True
        try:
            results = await asyncio.gather(*(
                run_blocking(self, actuator.stop) for actuator in self._actuators.values()
            ), return_exceptions=True)
            failures = [
                (axis, result) for axis, result in zip(self._actuators, results)
                if isinstance(result, BaseException)
            ]
            if failures:
                detail = "; ".join(f"{axis}: {error}" for axis, error in failures)
                raise RuntimeError(f"Hexapod stop failed ({detail})") from failures[0][1]
            await self._motion.refresh()
        except BaseException:
            self._motion.watch()
            raise

    def get_capability(self, name: str) -> Optional[Any]:
        if name == "actuators":
            return self._actuators
        return super().get_capability(name)
