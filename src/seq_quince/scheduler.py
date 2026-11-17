from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from seq_quince.device.device_abs import DeviceABC
from seq_quince.device.lifecycle import mark_unconfirmed, run_operation, track_task
from seq_quince.sequence import MoveStep

log = logging.getLogger("seq_quince.scheduler")


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #

class MoveError(RuntimeError):
    """Any fatal failure while executing a MoveStep."""

class RollbackError(RuntimeError):
    """Raised if rollback itself fails – LAST resort."""


# --------------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------------- #

class Scheduler:
    """
    Drive a *Sequence* (list[MoveStep]) asynchronously, with:

    * parallel execution per `group_id`
    * optional per-step timeout
    * best-effort rollback on individually completed steps that opted in
      (``MoveStep.rollback``, off by default)

    On abort, stop requests and original operations have ``cleanup_timeout``
    seconds to settle naturally. Remaining coroutines are cancelled and given
    one more such interval. Cancellation is not evidence that hardware stopped:
    unconfirmed devices are reported and excluded from rollback. Each rollback
    has the same execution deadline and, if needed, the same stop/settle phases.
    These bounds require device methods to yield rather than block the loop.
    """

    def __init__(
        self,
        steps: Sequence[MoveStep],
        *,
        step_timeout: Optional[float] = None,
        log_level: int = logging.WARNING,
        cleanup_timeout: float = 5.0,
    ) -> None:
        if not math.isfinite(cleanup_timeout) or cleanup_timeout <= 0:
            raise ValueError("cleanup_timeout must be finite and positive")
        log.setLevel(log_level)
        self._steps = list(steps)
        self._finished: list[MoveStep] = []
        self._step_timeout = step_timeout
        self._cleanup_timeout = cleanup_timeout
        self._rollback_snapshots: Dict[int, Dict[str, Any]] = {}
        self._group_tasks: Dict[asyncio.Task, MoveStep] = {}
        self._completed_steps: Set[int] = set()
        self._unconfirmed: Dict[int, DeviceABC] = {}
        self._background_tasks: Set[asyncio.Task] = set()

    @property
    def unconfirmed_devices(self) -> Tuple[str, ...]:
        """Devices whose stop/operation settlement could not be confirmed."""
        return tuple(device.name for device in self._unconfirmed.values())

    # ------------------------------------------------------------ public API
    async def run(self) -> None:
        """
        Execute the sequence.

        Raise MoveError from the original failure after bounded cleanup and
        best-effort rollback. Cancellation runs the same cleanup, then propagates
        CancelledError, including cancellation received during failure recovery.
        """
        try:
            for group_id, group in self._by_group():
                if group_id is None:         # barrier marker
                    log.info("Barrier reached – waiting for moves to finish")
                    continue

                await self._capture_group_rollback_snapshots(group)
                await self._run_group(group)

        except (Exception, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                log.warning("Sequence cancelled; stopping devices before rollback")
            else:
                log.exception("Sequence failed; stopping devices before rollback")
            await self._recover(exc)
            message = "Sequence aborted; rollback attempted (best effort)"
            if self.unconfirmed_devices:
                message += "; devices not confirmed stopped: " + ", ".join(self.unconfirmed_devices)
            raise MoveError(message) from exc

    async def snapshot(self) -> Dict[str, Dict[str, float]]:
        """Return {device_name: {axis: rbv, …}} for every device in the run."""
        devices = {step.device for step in self._steps
                   if not step.device.name.startswith("__barrier__")}
        snapshots = await asyncio.gather(*(self._snapshot_device(d) for d in devices))
        return {d.name: snap for d, snap in zip(devices, snapshots)}

    async def _snapshot_device(self, device: DeviceABC) -> Dict[str, float]:
        try:
            return await run_operation(device, device.snapshot())
        except (Exception, asyncio.CancelledError):
            # An extension may have abandoned executor children before raising.
            self._mark_unconfirmed(device, "snapshot failed or cancelled; internal work may remain")
            raise

    # ------------------------------------------------------------ internals
    def _by_group(self):
        """Yield (group_id, [MoveStep,…]) in input order."""
        groups: Dict[int | None, List[MoveStep]] = {}
        for step in self._steps:
            groups.setdefault(step.group_id, []).append(step)
        # Preserve original grouping order
        for gid in sorted(groups, key=lambda g: (-1 if g is None else g)):
            yield gid, groups[gid]

    async def _run_group(self, group: Sequence[MoveStep]) -> None:
        """Monitor original operations without cancelling executor waiters."""
        async def _one(step: MoveStep):
            log.debug("▶ %s", step)
            await step.device.process(step.pose)
            self._completed_steps.add(id(step))
            log.debug("✔ %s", step)

        timeouts = [self._effective_timeout(step) for step in group]
        loop = asyncio.get_running_loop()
        deadlines: Dict[asyncio.Task, float] = {}
        for step, timeout in zip(group, timeouts):
            task = asyncio.create_task(_one(step))
            track_task(step.device, task)
            self._group_tasks[task] = step
            if timeout is not None:
                deadlines[task] = loop.time() + timeout
        pending = set(self._group_tasks)
        try:
            while pending:
                remaining = [deadlines[t] - loop.time() for t in pending if t in deadlines]
                timeout = max(0.0, min(remaining)) if remaining else None
                done, pending = await asyncio.wait(
                    pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                for task in self._group_tasks:
                    if task in done:
                        task.result()
                if any(deadlines[t] <= loop.time() for t in pending if t in deadlines):
                    raise asyncio.TimeoutError("Device step timed out")
        finally:
            # Preserve configured rollback order, including successes in a
            # group whose sibling failed. Stop-induced returns are not successes.
            self._finished.extend(step for step in group if id(step) in self._completed_steps)
        self._group_tasks.clear()

    async def _recover(self, original: BaseException) -> None:
        """Protect one recovery task from repeated caller cancellation."""
        cancelled = original if isinstance(original, asyncio.CancelledError) else None
        recovery = asyncio.create_task(self._abort())
        while not recovery.done():
            try:
                await asyncio.shield(recovery)
            except asyncio.CancelledError as exc:
                cancelled = exc
            except Exception:
                log.debug("Recovery task raised; preserving original failure", exc_info=True)
                break
        try:
            recovery.result()
        except (Exception, asyncio.CancelledError):
            log.exception("Sequence recovery failed")
        if cancelled is not None:
            raise cancelled

    async def _abort(self) -> None:
        await self._stop_and_settle({task: step.device for task, step in self._group_tasks.items()})
        self._group_tasks.clear()
        await self._rollback()

    async def _stop_and_settle(self, operations: Dict[asyncio.Task, DeviceABC]) -> None:
        """Stop each device independently; require natural operation settlement.

        Never infer physical completion from a cancelled coroutine. In particular,
        cancelling a run_in_executor waiter does not stop its worker or hardware.
        """
        if not operations:
            return
        devices = {id(device): device for device in operations.values()}

        async def _stop(device: DeviceABC) -> None:
            track_task(device, asyncio.current_task())
            await device.stop()

        stops = {asyncio.create_task(_stop(device)): device for device in devices.values()}
        _, pending = await asyncio.wait(
            set(operations) | set(stops), timeout=self._cleanup_timeout
        )
        for task, device in operations.items():
            if not task.done() or task.cancelled():
                self._mark_unconfirmed(device, "original operation did not settle naturally")
            elif task.exception() is not None:
                # A driver's gather() may raise while internal executor jobs are
                # still queued. Even busy=False cannot exclude a later dispatch.
                self._mark_unconfirmed(device, "operation failed; internal work may remain")
        for task, device in stops.items():
            if not task.done() or task.cancelled():
                self._mark_unconfirmed(device, "stop request did not settle", hold=True)
            elif task.exception() is not None:
                log.error("Stop failed for %s: %s", device.name, task.exception())
                self._mark_unconfirmed(device, "stop request failed", hold=True)
        # Check before coroutine cancellation: some devices clear busy in finally
        # while their executor worker is still running.
        for device in devices.values():
            try:
                if device.busy:
                    self._mark_unconfirmed(device, "device is still busy")
            except Exception:
                log.exception("Could not read busy state for %s", device.name)
                self._mark_unconfirmed(device, "busy state unavailable", hold=True)
        await self._cancel_and_settle(pending)
        for task in set(operations) | set(stops):
            if task.done() and not task.cancelled():
                task.exception()  # Retrieve sibling failures as well as the first.

    def _mark_unconfirmed(self, device: DeviceABC, reason: str, *, hold: bool = False) -> None:
        """``hold``: disposal must not close the device, see mark_unconfirmed()."""
        self._unconfirmed[id(device)] = device
        mark_unconfirmed(device, reason, hold=hold)
        log.error("Device %s not confirmed stopped (%s); skipping its rollback", device.name, reason)

    async def _cancel_and_settle(self, pending: Set[asyncio.Task]) -> None:
        if not pending:
            return
        for task in pending:
            task.cancel()
        _, unsettled = await asyncio.wait(pending, timeout=self._cleanup_timeout)
        # A coroutine can suppress cancellation. Retain and observe such tasks;
        # the device remains excluded from rollback even if it finishes later.
        for task in unsettled:
            self._background_tasks.add(task)
            task.add_done_callback(self._observe_background_task)
        if unsettled:
            log.error("%d cleanup coroutine(s) did not settle after cancellation", len(unsettled))

    def _observe_background_task(self, task: asyncio.Task) -> None:
        self._background_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def _effective_timeout(self, step: MoveStep) -> Optional[float]:
        """Resolve scheduler-enforced timeout for a step."""
        timeout = step.device.scheduler_timeout(step.pose)
        if timeout is None:
            timeout = self._step_timeout
        if timeout is None:
            return None
        timeout = float(timeout)
        if timeout <= 0:
            return None
        return timeout

    def _supports_rollback(self, device: DeviceABC) -> bool:
        supports_rollback = getattr(device, "supports_rollback", None)
        if callable(supports_rollback):
            return bool(supports_rollback())
        return True

    def _rollback_enabled(self, step: MoveStep) -> bool:
        """A step is rolled back only if it opted in and its device supports it."""
        return step.rollback and self._supports_rollback(step.device)

    # --------------------------------------------------------- rollback path
    async def _rollback(self) -> None:
        """Best-effort: drive completed opted-in steps back to their pre-run snapshot pose."""
        for step in reversed(self._finished):
            if not step.rollback:
                log.info("Skipping rollback for %s (rollback not enabled for this step)",
                         step.device.name)
                continue
            if id(step.device) in self._unconfirmed:
                log.warning("Skipping rollback for %s (stop unconfirmed)", step.device.name)
                continue
            try:
                if not self._supports_rollback(step.device):
                    log.info("Skipping rollback for %s (rollback unsupported)", step.device.name)
                    continue
                rbv = self._rollback_snapshots.get(id(step))
                if rbv is None:
                    log.warning("No rollback snapshot available for %s; skipping", step.device.name)
                    continue
                await self._rollback_step(step.device, rbv)
            except (Exception, asyncio.CancelledError):
                log.exception("Rollback failed for %s", step.device.name)
                # keep going – do not raise until all attempted
        log.warning("Rollback completed (best effort)")

    async def _rollback_step(self, device: DeviceABC, pose: Dict[str, Any]) -> None:
        task = asyncio.create_task(device.process(pose))
        track_task(device, task)
        done, _ = await asyncio.wait({task}, timeout=self._cleanup_timeout)
        if not done:
            log.error("Rollback timed out for %s", device.name)
            await self._stop_and_settle({task: device})
        elif task.cancelled() or task.exception() is not None:
            await self._stop_and_settle({task: device})
            task.result()

    async def _capture_group_rollback_snapshots(self, group: Sequence[MoveStep]) -> None:
        """Capture pre-run snapshots for steps in the group that will roll back."""
        async def _capture(step: MoveStep) -> None:
            if not self._rollback_enabled(step):
                return
            self._rollback_snapshots[id(step)] = await self._snapshot_device(step.device)

        await asyncio.gather(*(_capture(step) for step in group))


