from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from threading import Event
from typing import Any, Dict, Mapping, Optional

from epics.ca import ChannelAccessException
from kiwi_scan.epics_wrapper import EpicsPV

from seq_quince.device.device_abs import DeviceABC, get_param
from seq_quince.device.lifecycle import (
    ensure_open,
    has_pending_work,
    run_blocking,
    track_resource,
)
from seq_quince.device_concrete._option_lookup import first_present, value_from_pose

logger = logging.getLogger(__name__)

_AXES = ("alpha", "beta", "theta")
_VALID_MODES = {"stddev", "spread", "reference"}


def _required_string(config: Mapping[str, Any], key: str) -> str:
    value = get_param(dict(config), key, str, required=True)
    if value is None:
        raise ValueError(f"Missing required parameter: '{key}'")
    return value

@dataclass(frozen=True)
class ZeroOrderOptions:
    mode: str
    in_position_band: float
    poll_period: float
    stable_reads: int
    timeout: float
    log_every: int
    pv_read_timeout: float
    read_retry_count: int
    max_consecutive_read_failures: int
    reference_alpha: Optional[float] = None
    reference_beta: Optional[float] = None
    reference_theta: Optional[float] = None

    def reference_dict(self) -> Dict[str, float]:
        ref: Dict[str, float] = {}
        if self.reference_alpha is not None:
            ref["alpha"] = float(self.reference_alpha)
        if self.reference_beta is not None:
            ref["beta"] = float(self.reference_beta)
        if self.reference_theta is not None:
            ref["theta"] = float(self.reference_theta)
        return ref


class MonoZeroOrderDevice(DeviceABC):
    """
    Wait-only scheduler device for monochromator zero-order detection.

    The device does not command motion. `process()` blocks until the monitored
    alpha/beta/theta PVs satisfy one of three configurable tests:

      - stddev:    standard deviation of alpha, beta, theta
      - spread:    max(alpha, beta, theta) - min(alpha, beta, theta)
      - reference: all three positions are within `in_position_band` of a
                   reference triplet

    `process()` accepts runtime overrides either as top-level pose keys or
    inside pose["options"]. Top-level keys win.
    TODO: dataclass based constructor using ZeroOrderOptions
    """
    _cleanup_tracks_work = True


    def __init__( # pylint: disable=too-many-locals
        self,
        name: str,
        alpha_pv: str,
        beta_pv: str,
        theta_pv: str,
        watchdog_pv: Optional[str] = None,
        pause_pv: Optional[str] = None,
        *,
        mode: str = "stddev",
        in_position_band: float = 0.005,
        poll_period: float = 0.2,
        stable_reads: int = 3,
        timeout: float = 60.0,
        log_every: int = 10,
        pv_read_timeout: float = 0.5,
        read_retry_count: int = 3,
        max_consecutive_read_failures: int = 5,
        reference: Optional[Mapping[str, float]] = None,
    ) -> None:
        self.name = name
        self.alpha_pv = EpicsPV(alpha_pv)
        track_resource(self, self.alpha_pv, self.alpha_pv.close)
        self.beta_pv = EpicsPV(beta_pv)
        track_resource(self, self.beta_pv, self.beta_pv.close)
        self.theta_pv = EpicsPV(theta_pv)
        track_resource(self, self.theta_pv, self.theta_pv.close)
        self.watchdog_pv = EpicsPV(watchdog_pv) if watchdog_pv else None
        if self.watchdog_pv is not None:
            track_resource(self, self.watchdog_pv, self.watchdog_pv.close)
        self.pause_pv = EpicsPV(pause_pv) if pause_pv else None
        if self.pause_pv is not None:
            track_resource(self, self.pause_pv, self.pause_pv.close)

        ref = dict(reference or {})
        self.mode = self._normalize_mode(mode)
        self.in_position_band = float(in_position_band)
        self.poll_period = float(poll_period)
        self.stable_reads = max(1, int(stable_reads))
        self.timeout = float(timeout)
        self.log_every = max(1, int(log_every))
        self.pv_read_timeout = float(pv_read_timeout)
        self.read_retry_count = max(1, int(read_retry_count))
        self.max_consecutive_read_failures = max(1, int(max_consecutive_read_failures))
        self.reference_alpha = self._maybe_float(ref.get("alpha"))
        self.reference_beta = self._maybe_float(ref.get("beta"))
        self.reference_theta = self._maybe_float(ref.get("theta"))

        self._busy = False
        # Polled stop flag: construction must not require an asyncio event loop.
        self._stop_event = Event()

        self._validate_options(self._default_options())

        logger.info(
            "[%s] configured zero-order monitor: mode=%s band=%s poll_period=%ss stable_reads=%s timeout=%ss log_every=%s watchdog=%s pause=%s",
            self.name,
            self.mode,
            self.in_position_band,
            self.poll_period,
            self.stable_reads,
            self.timeout,
            self.log_every,
            bool(self.watchdog_pv),
            bool(self.pause_pv),
        )

    @classmethod
    def from_config_dict(cls, config: Dict[str, Any]) -> MonoZeroOrderDevice:
        config_options = get_param(config, "options", dict, default={}) or {}
        band = cls._first_present(config, config_options, keys=("in_position_band", "deviation_threshold"), default=0.005)
        reference = get_param(config, "reference", dict, default=None)
        if reference is None:
            reference = config_options.get("reference")
        return cls(
            name=_required_string(config, "name"),
            alpha_pv=_required_string(config, "alpha_pv"),
            beta_pv=_required_string(config, "beta_pv"),
            theta_pv=_required_string(config, "theta_pv"),
            watchdog_pv=get_param(config, "watchdog_pv", str, default=None),
            pause_pv=get_param(config, "pause_pv", str, default=None),
            mode=cls._first_present(config, config_options, keys=("mode",), default="stddev"),
            in_position_band=float(band),
            poll_period=float(cls._first_present(config, config_options, keys=("poll_period",), default=0.2)),
            stable_reads=int(cls._first_present(config, config_options, keys=("stable_reads",), default=3)),
            timeout=float(cls._first_present(config, config_options, keys=("timeout",), default=60.0)),
            log_every=int(cls._first_present(config, config_options, keys=("log_every",), default=10)),
            pv_read_timeout=float(cls._first_present(config, config_options, keys=("pv_read_timeout",), default=0.5)),
            read_retry_count=int(cls._first_present(config, config_options, keys=("read_retry_count",), default=3)),
            max_consecutive_read_failures=int(cls._first_present(config, config_options, keys=("max_consecutive_read_failures",), default=5)),
            reference=reference,
        )

    async def _kick_watchdog(self) -> None:
        if self.watchdog_pv is None:
            return
        await run_blocking(self, self.watchdog_pv.put, 0)

    async def _wait_while_paused(self, options: ZeroOrderOptions) -> None:
        if self.pause_pv is None:
            return

        paused_logged = False
        while True:
            if self._stop_event.is_set():
                raise RuntimeError(f"{self.name}: zero-order wait aborted while paused")

            pause_value = await run_blocking(self, self.pause_pv.get)
            pause_value = self._require_float(pause_value, "Pause")
            await self._kick_watchdog()
            if pause_value == 0:
                if paused_logged:
                    logger.info("[%s] pause released, resuming zero-order detection", self.name)
                return

            if not paused_logged:
                logger.info("[%s] pause active (pause_pv=%s), waiting until value returns to 0", self.name, pause_value)
                paused_logged = True
            else:
                logger.debug("[%s] still paused (pause_pv=%s)", self.name, pause_value)

            await asyncio.sleep(options.poll_period)

    async def process(self, pose: Dict[str, float]) -> None:
        ensure_open(self)
        if self.busy:
            raise RuntimeError(f"{self.name}: zero-order monitor is already active")

        options = self._resolve_options(pose)
        self._validate_options(options)

        self._busy = True
        self._stop_event.clear()
        sample = 0
        stable_count = 0
        was_in_position = False
        consecutive_read_failures = 0
        deadline = None if options.timeout <= 0 else asyncio.get_running_loop().time() + options.timeout

        logger.info(
            "[%s] waiting for zero order: mode=%s band=%s stable_reads=%s timeout=%ss",
            self.name,
            options.mode,
            options.in_position_band,
            options.stable_reads,
            options.timeout,
        )

        try:
            while True:
                if self._stop_event.is_set():
                    raise RuntimeError(f"{self.name}: zero-order wait aborted")

                await self._kick_watchdog()
                await self._wait_while_paused(options)

                try:
                    values = await self._read_values_async(options)
                except Exception as exc:
                    consecutive_read_failures += 1
                    logger.warning(
                        "[%s] PV read failed (%d/%d): %s",
                        self.name,
                        consecutive_read_failures,
                        options.max_consecutive_read_failures,
                        exc,
                    )
                    if consecutive_read_failures >= options.max_consecutive_read_failures:
                        raise RuntimeError(
                            f"{self.name}: exceeded maximum consecutive PV read failures "
                            f"({options.max_consecutive_read_failures})"
                        ) from exc
                    await asyncio.sleep(options.poll_period)
                    continue

                consecutive_read_failures = 0
                metric = self._metric(values, options)
                in_position = math.isfinite(metric) and metric <= options.in_position_band
                stable_count = stable_count + 1 if in_position else 0
                sample += 1

                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(self._format_sample(sample, values, metric, options, stable_count, in_position))
                elif sample == 1 or sample % options.log_every == 0 or in_position != was_in_position:
                    logger.info(self._format_sample(sample, values, metric, options, stable_count, in_position))

                if stable_count >= options.stable_reads:
                    logger.info(
                        "[%s] zero order detected after %d samples: mode=%s metric=%.6g band=%.6g",
                        self.name,
                        sample,
                        options.mode,
                        metric,
                        options.in_position_band,
                    )
                    return

                if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError(
                        f"{self.name}: zero order not reached within {options.timeout}s "
                        f"(mode={options.mode}, last_metric={metric:.6g}, band={options.in_position_band:.6g})"
                    )

                was_in_position = in_position
                await asyncio.sleep(options.poll_period)

        except Exception:
            logger.exception("[%s] zero-order wait failed", self.name)
            raise
        finally:
            self._busy = False

    async def snapshot(self) -> Dict[str, float]:
        """Return the current live values. """
        ensure_open(self)
        return  await self._read_values_async(self._default_options())

    @property
    def busy(self) -> bool:
        return self._busy or has_pending_work(self)

    def supports_rollback(self) -> bool:
        return False

    def scheduler_timeout(self, pose: Optional[Dict[str, Any]] = None) -> Optional[float]:
        """Use the scheduler's global timeout in addition to the internal timeout."""
        return None

    async def stop(self) -> None:
        self._stop_event.set()
        for _ in range(20):
            if not self.busy:
                break
            await asyncio.sleep(0.05)

    async def _read_values_async(self, options: ZeroOrderOptions) -> Dict[str, float]:
        return await run_blocking(self, self._read_values, options)

    def _read_values(self, options: ZeroOrderOptions) -> Dict[str, float]:
        values = {
            "alpha": self._read_single_pv(self.alpha_pv, "alpha", options),
            "beta": self._read_single_pv(self.beta_pv, "beta", options),
            "theta": self._read_single_pv(self.theta_pv, "theta", options),
        }
        return values

    def _read_single_pv(self, pv: EpicsPV, label: str, options: ZeroOrderOptions) -> float:
        last_error: Optional[Exception] = None
        for attempt in range(1, options.read_retry_count + 1):
            try:
                value = pv.get(timeout=options.pv_read_timeout)
                return self._require_float(value, label)
            except (ConnectionError, ChannelAccessException, TypeError, ValueError) as exc:
                last_error = exc
                if attempt < options.read_retry_count:
                    logger.debug(
                        "[%s] read retry %d/%d for %s after error: %s",
                        self.name,
                        attempt,
                        options.read_retry_count,
                        label,
                        exc,
                    )
        raise ValueError(f"Could not read a numeric value for {label} after {options.read_retry_count} attempts") from last_error

    def _default_options(self) -> ZeroOrderOptions:
        return ZeroOrderOptions(
            mode=self.mode,
            in_position_band=self.in_position_band,
            poll_period=self.poll_period,
            stable_reads=self.stable_reads,
            timeout=self.timeout,
            log_every=self.log_every,
            pv_read_timeout=self.pv_read_timeout,
            read_retry_count=self.read_retry_count,
            max_consecutive_read_failures=self.max_consecutive_read_failures,
            reference_alpha=self.reference_alpha,
            reference_beta=self.reference_beta,
            reference_theta=self.reference_theta,
        )

    def _resolve_options(self, pose: Optional[Mapping[str, Any]]) -> ZeroOrderOptions:
        pose = dict(pose or {})
        pose_options = pose.get("options")
        pose_options = dict(pose_options) if isinstance(pose_options, Mapping) else {}
        defaults = self._default_options()

        mode = self._normalize_mode(self._value_from_pose(pose, pose_options, "mode", defaults.mode))
        band = float(self._value_from_pose(
            pose,
            pose_options,
            "in_position_band",
            defaults.in_position_band,
            alias_keys=("deviation_threshold",),
        ))
        poll_period = float(self._value_from_pose(pose, pose_options, "poll_period", defaults.poll_period))
        stable_reads = int(self._value_from_pose(pose, pose_options, "stable_reads", defaults.stable_reads))
        timeout = float(self._value_from_pose(pose, pose_options, "timeout", defaults.timeout))
        log_every = int(self._value_from_pose(pose, pose_options, "log_every", defaults.log_every))
        pv_read_timeout = float(self._value_from_pose(pose, pose_options, "pv_read_timeout", defaults.pv_read_timeout))
        read_retry_count = int(self._value_from_pose(pose, pose_options, "read_retry_count", defaults.read_retry_count))
        max_consecutive_read_failures = int(self._value_from_pose(pose, pose_options, "max_consecutive_read_failures", defaults.max_consecutive_read_failures))

        reference = self._resolve_reference(pose, pose_options, defaults)

        return ZeroOrderOptions(
            mode=mode,
            in_position_band=band,
            poll_period=poll_period,
            stable_reads=stable_reads,
            timeout=timeout,
            log_every=log_every,
            pv_read_timeout=pv_read_timeout,
            read_retry_count=read_retry_count,
            max_consecutive_read_failures=max_consecutive_read_failures,
            reference_alpha=reference.get("alpha"),
            reference_beta=reference.get("beta"),
            reference_theta=reference.get("theta"),
        )

    @staticmethod
    def _resolve_reference(
        pose: Mapping[str, Any],
        pose_options: Mapping[str, Any],
        defaults: ZeroOrderOptions,
    ) -> Dict[str, float]:
        """Apply reference overrides in their existing precedence order."""
        reference = defaults.reference_dict()

        options_reference = pose_options.get("reference")
        if isinstance(options_reference, Mapping):
            for axis in _AXES:
                if axis in options_reference:
                    reference[axis] = float(options_reference[axis])

        pose_reference = pose.get("reference")
        if isinstance(pose_reference, Mapping):
            for axis in _AXES:
                if axis in pose_reference:
                    reference[axis] = float(pose_reference[axis])

        for axis in _AXES:
            option_key = f"reference_{axis}"
            if option_key in pose_options:
                reference[axis] = float(pose_options[option_key])
            if option_key in pose:
                reference[axis] = float(pose[option_key])

        if all(axis in pose for axis in _AXES):
            for axis in _AXES:
                reference[axis] = float(pose[axis])

        return reference

    def _metric(self, values: Mapping[str, float], options: ZeroOrderOptions) -> float:
        alpha = float(values["alpha"])
        beta = float(values["beta"])
        theta = float(values["theta"])

        if options.mode == "stddev":
            mean = (alpha + beta + theta) / 3.0
            return math.sqrt(((alpha - mean) ** 2 + (beta - mean) ** 2 + (theta - mean) ** 2) / 3.0)

        if options.mode == "spread":
            return max(alpha, beta, theta) - min(alpha, beta, theta)

        if options.mode == "reference":
            reference = options.reference_dict()
            missing = [axis for axis in _AXES if axis not in reference]
            if missing:
                raise ValueError(
                    f"{self.name}: reference mode requires reference values for {missing}. "
                    "Provide them in config['reference'], pose['reference'], reference_alpha/beta/theta, "
                    "or alpha/beta/theta in the pose."
                )
            return max(
                abs(alpha - reference["alpha"]),
                abs(beta - reference["beta"]),
                abs(theta - reference["theta"]),
            )

        raise ValueError(f"{self.name}: unknown zero-order mode '{options.mode}'")

    def _format_sample(
        self,
        sample: int,
        values: Mapping[str, float],
        metric: float,
        options: ZeroOrderOptions,
        stable_count: int,
        in_position: bool,
    ) -> str:
        parts = [
            f"[{self.name}] sample={sample}",
            f"mode={options.mode}",
            f"alpha={values['alpha']:.6f}",
            f"beta={values['beta']:.6f}",
            f"theta={values['theta']:.6f}",
            f"metric={metric:.6g}",
            f"band={options.in_position_band:.6g}",
            f"stable={stable_count}/{options.stable_reads}",
            f"in_position={in_position}",
        ]
        if options.mode == "reference":
            reference = options.reference_dict()
            parts.extend([
                f"ref_alpha={reference['alpha']:.6f}",
                f"ref_beta={reference['beta']:.6f}",
                f"ref_theta={reference['theta']:.6f}",
            ])
        return " ".join(parts)

    _value_from_pose = staticmethod(value_from_pose)
    _first_present = staticmethod(first_present)

    @staticmethod
    def _normalize_mode(mode: Any) -> str:
        value = str(mode).strip().lower()
        if value not in _VALID_MODES:
            raise ValueError(f"Invalid mode '{mode}'. Valid modes: {sorted(_VALID_MODES)}")
        return value

    @staticmethod
    def _require_float(value: Any, label: str) -> float:
        try:
            return float(value)
        except Exception as exc:
            raise ValueError(f"Could not read a numeric value for {label}: {value!r}") from exc

    @staticmethod
    def _maybe_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        return float(value)

    def _validate_options(self, options: ZeroOrderOptions) -> None:
        if options.in_position_band < 0:
            raise ValueError(f"{self.name}: in_position_band must be >= 0")
        if options.poll_period <= 0:
            raise ValueError(f"{self.name}: poll_period must be > 0")
        if options.stable_reads < 1:
            raise ValueError(f"{self.name}: stable_reads must be >= 1")
        if options.log_every < 1:
            raise ValueError(f"{self.name}: log_every must be >= 1")
        if options.pv_read_timeout <= 0:
            raise ValueError(f"{self.name}: pv_read_timeout must be > 0")
        if options.read_retry_count < 1:
            raise ValueError(f"{self.name}: read_retry_count must be >= 1")
        if options.max_consecutive_read_failures < 1:
            raise ValueError(f"{self.name}: max_consecutive_read_failures must be >= 1")
