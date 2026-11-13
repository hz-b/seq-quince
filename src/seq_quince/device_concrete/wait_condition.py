from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from kiwi_scan.epics_wrapper import EpicsPV

from seq_quince.device.device_abs import DeviceABC, get_param
from seq_quince.device.lifecycle import (
    ensure_open,
    has_pending_work,
    run_blocking,
    track_resource,
)
from seq_quince.device_concrete._option_lookup import first_present, value_from_pose

log = logging.getLogger(__name__)


_CONDITION_ALIASES = {
    "==": "eq",
    "eq": "eq",
    "equals": "eq",
    "equal": "eq",
    "!=": "ne",
    "ne": "ne",
    "not_equal": "ne",
    "not_equals": "ne",
    ">": "gt",
    "gt": "gt",
    "greater": "gt",
    "greater_than": "gt",
    ">=": "ge",
    "ge": "ge",
    "gte": "ge",
    "greater_equal": "ge",
    "greater_or_equal": "ge",
    "less_or_equal": "le",
    "<": "lt",
    "lt": "lt",
    "less": "lt",
    "less_than": "lt",
    "<=": "le",
    "le": "le",
    "lte": "le",
    "less_equal": "le",
}


@dataclass(frozen=True)
class WaitConditionOptions:
    pv: str
    value: Any
    condition: str
    poll_period: float
    timeout: float
    pv_read_timeout: float
    read_retry_count: int
    max_consecutive_read_failures: int
    log_every: int
    watchdog_pv: Optional[str] = None
    watchdog_frequency: Optional[float] = None

    @property
    def watchdog_enabled(self) -> bool:
        return bool(self.watchdog_pv)

    @property
    def watchdog_period(self) -> Optional[float]:
        if not self.watchdog_enabled or self.watchdog_frequency is None:
            return None
        return 1.0 / self.watchdog_frequency


class WaitConditionDevice(DeviceABC):
    """
    Generic wait-only scheduler device.

    `process()` blocks until the current value of a PV satisfies a comparison
    against an expected value.

    Required runtime inputs:
      - pv
      - value
      - condition

    Optional watchdog support:
      - watchdog_pv
      - watchdog_frequency (Hz)

    The watchdog, when enabled, writes 0 immediately and then periodically at
    the configured frequency while the wait is active.
    """
    _cleanup_tracks_work = True


    def __init__(
        self,
        name: str,
        *,
        pv: Optional[str] = None,
        value: Any = None,
        condition: str = "eq",
        poll_period: float = 0.2,
        timeout: float = 0.0,
        pv_read_timeout: float = 0.5,
        read_retry_count: int = 3,
        max_consecutive_read_failures: int = 5,
        log_every: int = 10,
        watchdog_pv: Optional[str] = None,
        watchdog_frequency: Optional[float] = None,
    ) -> None:
        self.name = name
        self.default_pv = pv
        self.default_value = value
        self.default_condition = self._normalize_condition(condition)
        self.poll_period = float(poll_period)
        self.timeout = float(timeout)
        self.pv_read_timeout = float(pv_read_timeout)
        self.read_retry_count = max(1, int(read_retry_count))
        self.max_consecutive_read_failures = max(1, int(max_consecutive_read_failures))
        self.log_every = max(1, int(log_every))
        self.default_watchdog_pv = watchdog_pv
        self.default_watchdog_frequency = None if watchdog_frequency is None else float(watchdog_frequency)

        self._busy = False
        # Python 3.8 binds asyncio.Event at construction. Create it in process,
        # on the execution loop, rather than in a runner's construction worker.
        self._stop_event: Optional[asyncio.Event] = None
        self._pv_cache: Dict[str, EpicsPV] = {}

        self._validate_common(
            poll_period=self.poll_period,
            timeout=self.timeout,
            pv_read_timeout=self.pv_read_timeout,
            read_retry_count=self.read_retry_count,
            max_consecutive_read_failures=self.max_consecutive_read_failures,
            log_every=self.log_every,
            watchdog_pv=self.default_watchdog_pv,
            watchdog_frequency=self.default_watchdog_frequency,
        )

    @classmethod
    def from_config_dict(cls, config: Dict[str, Any]) -> WaitConditionDevice:
        options = get_param(config, "options", dict, default={}) or {}
        name=get_param(config, "name", str, required=True)
        if name == None:
            raise ValueError("Required parameter `name` is None")
        return cls(
            name=name,
            pv=cls._first_present(config, options, keys=("pv",), default=None),
            value=cls._first_present(config, options, keys=("value",), default=None),
            condition=cls._first_present(config, options, keys=("condition",), default="eq"),
            poll_period=float(cls._first_present(config, options, keys=("poll_period",), default=0.2)),
            timeout=float(cls._first_present(config, options, keys=("timeout",), default=0.0)),
            pv_read_timeout=float(cls._first_present(config, options, keys=("pv_read_timeout",), default=0.5)),
            read_retry_count=int(cls._first_present(config, options, keys=("read_retry_count",), default=3)),
            max_consecutive_read_failures=int(cls._first_present(config, options, keys=("max_consecutive_read_failures",), default=5)),
            log_every=int(cls._first_present(config, options, keys=("log_every",), default=10)),
            watchdog_pv=cls._first_present(config, options, keys=("watchdog_pv",), default=None),
            watchdog_frequency=cls._first_present(
                config,
                options,
                keys=("watchdog_frequency", "frequency"),
                default=None,
            ),
        )

    async def process(self, pose: Dict[str, Any]) -> None:
        ensure_open(self)
        if self.busy:
            raise RuntimeError(f"{self.name}: wait-condition device is already active")

        options = self._resolve_options(pose)
        self._validate_options(options)

        self._busy = True
        self._stop_event = asyncio.Event()
        watchdog_task: Optional[asyncio.Task] = None
        consecutive_read_failures = 0
        sample = 0
        deadline = None if options.timeout <= 0 else asyncio.get_running_loop().time() + options.timeout

        try:
            if options.watchdog_enabled:
                watchdog_task = asyncio.create_task(self._watchdog_loop(options), name=f"{self.name}-watchdog")

            log.info(
                "[%s] waiting for PV condition: pv=%s condition=%s value=%r timeout=%ss watchdog=%s freq=%sHz",
                self.name,
                options.pv,
                options.condition,
                options.value,
                options.timeout,
                options.watchdog_pv,
                options.watchdog_frequency,
            )

            while True:
                if self._stop_event.is_set():
                    raise RuntimeError(f"{self.name}: wait aborted")

                try:
                    current = await self._read_single_pv_async(options)
                except Exception as exc:
                    consecutive_read_failures += 1
                    log.warning(
                        "[%s] PV read failed (%d/%d) for %s: %s",
                        self.name,
                        consecutive_read_failures,
                        options.max_consecutive_read_failures,
                        options.pv,
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
                satisfied = self._evaluate_condition(current, options.value, options.condition)
                sample += 1

                self._log_sample(sample, current, options, satisfied)

                if satisfied:
                    log.info(
                        "[%s] condition met after %d samples: pv=%s current=%r condition=%s expected=%r",
                        self.name,
                        sample,
                        options.pv,
                        current,
                        options.condition,
                        options.value,
                    )
                    return

                if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError(
                        f"{self.name}: condition not met within {options.timeout}s "
                        f"(pv={options.pv}, last_value={current!r}, condition={options.condition}, expected={options.value!r})"
                    )

                await asyncio.sleep(options.poll_period)

        except Exception:
            log.exception("[%s] wait-condition failed", self.name)
            raise
        finally:
            self._stop_event.set()
            if watchdog_task is not None:
                watchdog_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watchdog_task
            self._busy = False

    def _log_sample(
        self,
        sample: int,
        current: Any,
        options: WaitConditionOptions,
        satisfied: bool,
    ) -> None:
        """Keep per-sample logging policy separate from the wait loop."""
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "[%s] sample=%d pv=%s current=%r condition=%s expected=%r satisfied=%s",
                self.name,
                sample,
                options.pv,
                current,
                options.condition,
                options.value,
                satisfied,
            )
        elif sample == 1 or sample % options.log_every == 0 or satisfied:
            log.info(
                "[%s] sample=%d pv=%s current=%r condition=%s expected=%r satisfied=%s",
                self.name,
                sample,
                options.pv,
                current,
                options.condition,
                options.value,
                satisfied,
            )

    async def snapshot(self) -> Dict[str, Any]:
        ensure_open(self)
        current_value = None
        if self.default_pv:
            try:
                pv = await run_blocking(self, self._get_pv, self.default_pv)
                current_value = await self._read_value_async(pv, self.pv_read_timeout)
            except Exception as exc:  # noqa: BLE001 -- Snapshot is best effort; report PV failures.
                log.warning("[%s] snapshot read failed for %s: %s", self.name, self.default_pv, exc)

        return {
            "pv": self.default_pv,
            "current_value": current_value,
            "value": self.default_value,
            "condition": self.default_condition,
            "poll_period": self.poll_period,
            "timeout": self.timeout,
            "watchdog_pv": self.default_watchdog_pv,
            "watchdog_frequency": self.default_watchdog_frequency,
        }

    @property
    def busy(self) -> bool:
        return self._busy or has_pending_work(self)

    def supports_rollback(self) -> bool:
        return False

    def scheduler_timeout(self, pose: Optional[Dict[str, Any]] = None) -> Optional[float]:
        # The internal timeout is separate; None inherits the scheduler's
        # global step_timeout when configured.
        return None

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        for _ in range(20):
            if not self.busy:
                break
            await asyncio.sleep(0.05)

    async def _watchdog_loop(self, options: WaitConditionOptions) -> None:
        stop_event = self._stop_event
        if stop_event is None:
            raise RuntimeError("Wait-condition execution has not started")
        if options.watchdog_pv == None:
            raise RuntimeError("`options.watchdog_pv` not defined")
        watchdog = await run_blocking(self, self._get_pv, options.watchdog_pv)
        period = options.watchdog_period
        if period is None:
            return

        while not stop_event.is_set():
            ok = await self._put_value_async(watchdog, 0)
            if ok is False:
                log.warning("[%s] failed to reset watchdog PV %s to 0", self.name, options.watchdog_pv)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=period)
            except asyncio.TimeoutError:
                continue

    async def _read_single_pv_async(self, options: WaitConditionOptions) -> Any:
        pv = await run_blocking(self, self._get_pv, options.pv)
        last_error: Optional[Exception] = None
        for attempt in range(1, options.read_retry_count + 1):
            try:
                return await self._read_value_async(pv, options.pv_read_timeout)
            except Exception as exc:  # noqa: BLE001 -- Retry PV read failures; chain the final error.
                last_error = exc
                if attempt < options.read_retry_count:
                    log.debug(
                        "[%s] read retry %d/%d for %s after error: %s",
                        self.name,
                        attempt,
                        options.read_retry_count,
                        options.pv,
                        exc,
                    )
        raise ValueError(f"Could not read a value for {options.pv} after {options.read_retry_count} attempts") from last_error

    async def _read_value_async(self, pv: EpicsPV, timeout: float) -> Any:
        return await run_blocking(self, functools.partial(pv.get, timeout=timeout))

    async def _put_value_async(self, pv: EpicsPV, value: Any) -> Any:
        return await run_blocking(self, pv.put, value)

    def _get_pv(self, pv_name: str) -> EpicsPV:
        if pv_name not in self._pv_cache:
            self._pv_cache[pv_name] = EpicsPV(pv_name)
            track_resource(self, self._pv_cache[pv_name], self._pv_cache[pv_name].close)
        return self._pv_cache[pv_name]

    def _resolve_options(self, pose: Optional[Mapping[str, Any]]) -> WaitConditionOptions:
        pose = dict(pose or {})
        pose_options = pose.get("options")
        pose_options = dict(pose_options) if isinstance(pose_options, Mapping) else {}

        pv = self._value_from_pose(pose, pose_options, "pv", self.default_pv)
        value = self._value_from_pose(pose, pose_options, "value", self.default_value)
        condition = self._normalize_condition(
            self._value_from_pose(pose, pose_options, "condition", self.default_condition)
        )
        poll_period = float(self._value_from_pose(pose, pose_options, "poll_period", self.poll_period))
        timeout = float(self._value_from_pose(pose, pose_options, "timeout", self.timeout))
        pv_read_timeout = float(self._value_from_pose(pose, pose_options, "pv_read_timeout", self.pv_read_timeout))
        read_retry_count = int(self._value_from_pose(pose, pose_options, "read_retry_count", self.read_retry_count))
        max_consecutive_read_failures = int(
            self._value_from_pose(
                pose,
                pose_options,
                "max_consecutive_read_failures",
                self.max_consecutive_read_failures,
            )
        )
        log_every = int(self._value_from_pose(pose, pose_options, "log_every", self.log_every))
        watchdog_pv = self._value_from_pose(pose, pose_options, "watchdog_pv", self.default_watchdog_pv)
        watchdog_frequency = self._value_from_pose(
            pose,
            pose_options,
            "watchdog_frequency",
            self.default_watchdog_frequency,
            alias_keys=("frequency",),
        )

        if not watchdog_pv:
            watchdog_frequency = None
        elif watchdog_frequency is not None:
            watchdog_frequency = float(watchdog_frequency)

        return WaitConditionOptions(
            pv=pv,
            value=value,
            condition=condition,
            poll_period=poll_period,
            timeout=timeout,
            pv_read_timeout=pv_read_timeout,
            read_retry_count=read_retry_count,
            max_consecutive_read_failures=max_consecutive_read_failures,
            log_every=log_every,
            watchdog_pv=watchdog_pv,
            watchdog_frequency=watchdog_frequency,
        )

    def _validate_options(self, options: WaitConditionOptions) -> None:
        if not options.pv:
            raise ValueError(f"{self.name}: 'pv' is required")
        if options.value is None:
            raise ValueError(f"{self.name}: 'value' is required")
        self._validate_common(
            poll_period=options.poll_period,
            timeout=options.timeout,
            pv_read_timeout=options.pv_read_timeout,
            read_retry_count=options.read_retry_count,
            max_consecutive_read_failures=options.max_consecutive_read_failures,
            log_every=options.log_every,
            watchdog_pv=options.watchdog_pv,
            watchdog_frequency=options.watchdog_frequency,
        )

    def _validate_common(
        self,
        *,
        poll_period: float,
        timeout: float,
        pv_read_timeout: float,
        read_retry_count: int,
        max_consecutive_read_failures: int,
        log_every: int,
        watchdog_pv: Optional[str],
        watchdog_frequency: Optional[float],
    ) -> None:
        if poll_period <= 0:
            raise ValueError(f"{self.name}: poll_period must be > 0")
        if timeout < 0:
            raise ValueError(f"{self.name}: timeout must be >= 0")
        if pv_read_timeout <= 0:
            raise ValueError(f"{self.name}: pv_read_timeout must be > 0")
        if read_retry_count < 1:
            raise ValueError(f"{self.name}: read_retry_count must be >= 1")
        if max_consecutive_read_failures < 1:
            raise ValueError(f"{self.name}: max_consecutive_read_failures must be >= 1")
        if log_every < 1:
            raise ValueError(f"{self.name}: log_every must be >= 1")
        if watchdog_frequency is not None and watchdog_frequency <= 0:
            raise ValueError(f"{self.name}: watchdog_frequency must be > 0")
        if watchdog_frequency is not None and not watchdog_pv:
            raise ValueError(
                f"{self.name}: watchdog_frequency was provided but watchdog_pv is missing"
            )

    @classmethod
    def _evaluate_condition(cls, current: Any, expected: Any, condition: str) -> bool:
        normalized = cls._normalize_condition(condition)
        lhs, rhs = cls._prepare_comparison_values(current, expected, normalized)

        if normalized == "eq":
            return lhs == rhs
        if normalized == "ne":
            return lhs != rhs
        if normalized == "gt":
            return lhs > rhs
        if normalized == "ge":
            return lhs >= rhs
        if normalized == "lt":
            return lhs < rhs
        if normalized == "le":
            return lhs <= rhs
        raise ValueError(f"Unsupported condition '{condition}'")

    @classmethod
    def _prepare_comparison_values(cls, current: Any, expected: Any, condition: str) -> tuple[Any, Any]:
        """ Convert values to numbers, allowing nonnumeric equality comparisons."""
        if condition in {"gt", "ge", "lt", "le"}:
            return cls._as_float(current), cls._as_float(expected)

        try:
            return cls._as_float(current), cls._as_float(expected)
        except (TypeError, ValueError):
            return cls._normalize_scalar(current), cls._normalize_scalar(expected)

    @staticmethod
    def _normalize_scalar(value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @staticmethod
    def _as_float(value: Any) -> float:
        return float(value)

    @classmethod
    def _normalize_condition(cls, condition: Any) -> str:
        key = str(condition).strip().lower()
        normalized = _CONDITION_ALIASES.get(key)
        if normalized is None:
            raise ValueError(f"Invalid condition '{condition}'. Valid conditions: {sorted(set(_CONDITION_ALIASES.keys()))}")
        return normalized
    
    # make _options helper available through the class and its instances
    # can be overridden
    _value_from_pose = staticmethod(value_from_pose)
    _first_present = staticmethod(first_present)
