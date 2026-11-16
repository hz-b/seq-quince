""" 
Aimed lifecycle pattern: explicit ownership, scoped cleanup, and tracked background work. 
Cancelling the Python caller does not prove that its hardware operation has stopped.
Avoid relying on __del__(), use explicit cleanup:
    try:
        await device.process(pose)
    finally:
        await device.close()
 TODO: 
 - Document ownership of shared and injected resources.
 - Apply lifecycle helpers consistently across all device adapters.
 - Define allowed lifecycle transitions and cleanup retry behavior.
 - Preserve execution errors while keeping cleanup failures visible.
 - Define runner shutdown behavior when devices remain retained.
"""
import asyncio
import logging
import math
import time
from contextvars import ContextVar, copy_context
from threading import RLock
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")
_current_scope: ContextVar = ContextVar("device_scope", default=None)
_retained: Dict[int, Any] = {}
_background = set()


class CleanupError(RuntimeError):
    """One or more devices could not be disposed within the cleanup budget."""


class _Resource:
    def __init__(self, resource, cleanup):
        self.resource = resource
        self.cleanup = cleanup
        self.future: Optional[asyncio.Future] = None


class _State:
    def __init__(self):
        self.work = set()
        self.active = set()
        self.resources: Dict[int, _Resource] = {}
        self.lock = RLock()
        self.close_task: Optional[asyncio.Task] = None
        self.closing = False
        self.closed = False
        self.unconfirmed = False
        self.hold: Optional[str] = None


def _state(owner) -> _State:
    # Resource registration may also happen in an executor worker. Initial
    # registration must precede dispatch; subsequent accesses reuse this state.
    state = getattr(owner, "_resource_lifecycle", None)
    if state is None:
        state = _State()
        owner._resource_lifecycle = state
    return state


def ensure_open(owner) -> None:
    """Reject use after successful disposal; close does not reopen devices."""
    if _state(owner).closed:
        raise RuntimeError(f"Device {owner.name!r} is closed")
    if _state(owner).closing:
        raise RuntimeError(f"Device {owner.name!r} is closing")


def own_device(device) -> None:
    """Register a constructed device with the active runner scope, if any."""
    scope = _current_scope.get()
    if scope is not None:
        scope.add(device)


def track_resource(owner, resource, cleanup: Callable[[], Any]) -> None:
    """
    Remember what needs closing:
    Register an exclusively owned resource immediately after allocation.
    """
    state = _state(owner)
    with state.lock:
        state.resources.setdefault(id(resource), _Resource(resource, cleanup))
    own_device(owner)


def track_task(owner, future) -> None:
    """Observe an operation, including after cancellation of its caller."""
    state = _state(owner)
    state.work.add(future)

    def finished(done):
        state.work.discard(done)
        if not done.cancelled():
            done.exception()  # Observe failures even if its waiter was cancelled.

    future.add_done_callback(finished)


async def run_blocking(owner, function: Callable[..., T], *args) -> T:
    """
    Remember work that is still running:
    Keep the executor future alive when its coroutine waiter is cancelled.
    """
    _state(owner)  # Initialize before the worker can register resources.
    future = asyncio.get_running_loop().run_in_executor(None, function, *args)
    track_task(owner, future)
    _track_active(owner, future)
    return await asyncio.shield(future)


async def run_operation(owner, operation: Awaitable[T]) -> T:
    """Track an async operation without abandoning it on waiter cancellation."""
    task = asyncio.ensure_future(operation)
    track_task(owner, task)
    _track_active(owner, task)
    return await asyncio.shield(task)


def _track_active(owner, future) -> None:
    state = _state(owner)
    with state.lock:
        state.active.add(future)

    def finished(done):
        with state.lock:
            state.active.discard(done)

    future.add_done_callback(finished)


def has_pending_work(owner) -> bool:
    """
    Check if work or motion remains, no hardware operation, non blocking.
    """
    state = _state(owner)
    # Check future for ongoing work that is not finished
    with state.lock:
        active = tuple(state.active)
    return any(not future.done() for future in active)


class MotionState:
    """
    Sample blocking readiness outside the loop; expose a conservative cache.

    A completed stop request only triggers a readiness sample. If motion is
    still reported, an owned observer polls until it ceases; disposal also
    waits for that observer. No constructor work depends on an event loop.
    """

    def __init__(self, owner, checker: Callable[[], bool]):
        self.owner = owner
        self.checker = checker
        self.busy = True  # Unknown until an async sample confirms readiness.
        self._observer: Optional[asyncio.Task] = None

    async def refresh(self) -> None:
        self.busy = True
        try:
            self.busy = bool(await run_blocking(self.owner, self.checker))
            self.busy = self.busy or has_pending_work(self.owner)
        except BaseException:
            self.watch()
            raise
        if self.busy:
            self.watch()

    def watch(self) -> None:
        """Continue observing after process cancellation, failure, or stop."""
        self.busy = True
        if self._observer is None or self._observer.done():
            self._observer = asyncio.create_task(self._observe())
            track_task(self.owner, self._observer)

    async def _observe(self) -> None:
        try:
            while True:
                # An abandoned worker may not have dispatched its move yet.
                # Sample again after all such work, even if hardware is idle now.
                # Work pending before the sample can finish while the sample is
                # in flight, so an idle result is only accepted when no work was
                # pending before the sample started or after it returned.
                pending = has_pending_work(self.owner)
                busy = bool(await run_blocking(self.owner, self.checker))
                self.busy = busy or pending or has_pending_work(self.owner)
                if not self.busy:
                    return
                await asyncio.sleep(0.05)
        except BaseException:
            self.busy = True
            mark_unconfirmed(self.owner, "busy state unavailable")
            raise


def mark_unconfirmed(owner, reason: str) -> None:
    """Carry scheduler uncertainty into disposal without changing rollback."""
    state = _state(owner)
    state.unconfirmed = True
    if (not type(owner).__dict__.get("_cleanup_tracks_work", False)
            or reason in {"stop request failed", "stop request did not settle", "busy state unavailable"}):
        state.hold = reason


def retained_devices() -> Tuple[Any, ...]:
    """Devices retained for outstanding work, cleanup errors, or uncertainty."""
    return tuple(_retained.values())


async def _settle(owner, state, *, exclude_current=False) -> None:
    current = asyncio.current_task() if exclude_current else None
    while state.work - {current}:
        await asyncio.wait(state.work - {current})
        # done callbacks remove work; let them run before checking again.
        await asyncio.sleep(0)
    if state.hold:
        raise CleanupError(f"{owner.name} retained: {state.hold}")
    if state.unconfirmed:
        # All tracked worker/stop/coroutine work must settle first. A cleared
        # coroutine busy flag alone never proves an executor worker finished.
        while True:
            try:
                # Query extensions outside the loop, but do not count the busy
                # query itself as hardware work in a built-in's busy property.
                future = asyncio.get_running_loop().run_in_executor(None, lambda: owner.busy)
                track_task(owner, future)
                busy = await asyncio.shield(future)
            except Exception as exc:
                state.hold = "busy state unavailable"
                raise CleanupError(f"{owner.name} retained: busy state unavailable") from exc
            if not busy:
                break
            await asyncio.sleep(0.05)


async def release_resource(owner, resource) -> None:
    """Dispose one resource; retain failed or interrupted attempts for retry."""
    state = _state(owner)
    await _settle(owner, state, exclude_current=True)
    with state.lock:
        entry = state.resources.get(id(resource))
        if entry is None:
            return
        if entry.future is None:
            entry.future = asyncio.get_running_loop().run_in_executor(None, entry.cleanup)
        future = entry.future
    try:
        await asyncio.shield(future)
    except asyncio.CancelledError:
        # A repeated close reuses this future instead of issuing another close.
        raise
    except Exception:
        with state.lock:
            entry.future = None
        raise
    else:
        with state.lock:
            state.resources.pop(id(resource), None)


async def _close_resources(owner, state) -> None:
    _retained[id(owner)] = owner
    try:
        await _settle(owner, state)
        failures = []
        with state.lock:
            resources = [entry.resource for entry in state.resources.values()]
        for resource in reversed(resources):
            try:
                await release_resource(owner, resource)
            except Exception as exc:
                failures.append(exc)
                log.exception("Resource disposal failed for %s", owner.name)
        if failures:
            raise CleanupError(f"Resource disposal failed for {owner.name}") from failures[0]
    except BaseException:
        log.error("Resources retained for %s; cleanup incomplete", owner.name)
        raise
    else:
        state.closed = True
        _retained.pop(id(owner), None)


async def close_resources(owner) -> None:
    """Idempotent, shielded disposal of resources registered by an adapter."""
    state = _state(owner)
    state.closing = True
    task = state.close_task
    if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
        task = asyncio.create_task(_close_resources(owner, state))
        state.close_task = task
        _keep_background(task, "Device cleanup")
    await asyncio.shield(task)


def _keep_background(task, description) -> None:
    _background.add(task)

    def done(finished):
        _background.discard(finished)
        if finished.cancelled():
            log.error("%s interrupted; retained resources may need manual disposal", description)
        elif finished.exception() is not None:
            log.error("%s failed: %s", description, finished.exception())

    task.add_done_callback(done)


class DeviceScope:
    """
    Arrange automatic cleanup for a whole run.
    Runner ownership, including devices partially constructed in this scope.
    """

    def __init__(self, timeout: float = 5.0):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("cleanup timeout must be finite and positive")
        self.timeout = timeout
        self.devices: Dict[int, Any] = {}
        self._lock = RLock()
        self._work = set()
        self._token = None

    def add(self, device) -> None:
        with self._lock:
            self.devices[id(device)] = device

    async def run_blocking(self, function: Callable[..., T], *args) -> T:
        """
        Construct in a worker with ownership context and survive cancellation.
        The scope waits for construction before enumerating its devices.
        """
        context = copy_context()

        def invoke() -> T:
            return context.run(function, *args)

        future = asyncio.get_running_loop().run_in_executor(None, invoke)
        self._work.add(future)

        def finished(done):
            self._work.discard(done)
            if not done.cancelled():
                done.exception()

        future.add_done_callback(finished)
        return await asyncio.shield(future)
    
    # Automatically called for: async with DeviceScope() as scope: ...
    # Save the token so __aexit__() can restore the previous scope.
    async def __aenter__(self):
        self._token = _current_scope.set(self)
        return self

    async def _dispose(self) -> None:
        while self._work:
            await asyncio.wait(self._work)
            await asyncio.sleep(0)
        with self._lock:
            devices = tuple(self.devices.values())

        async def close(device):
            _retained[id(device)] = device
            try:
                await _settle(device, _state(device))
                await device.close()
            except BaseException:
                log.exception("Device cleanup failed for %s", device.name)
                raise
            else:
                _state(device).closed = True
                _retained.pop(id(device), None)

        results = await asyncio.gather(*(close(d) for d in devices), return_exceptions=True)
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            names = ", ".join(d.name for d in devices if id(d) in _retained)
            raise CleanupError(f"Device cleanup failed: {names}") from failures[0]
    
    async def __aexit__(self, exc_type, exc, traceback):
        """ 
        On leaving the scope (exit the async with block): 
        start cleanup and wait up to the timeout.
        Keep unfinished cleanup running while the event loop remains alive.
        Preserve any original error or cancellation; 
        """
        if self._token is None:
            raise RuntimeError("DeviceScope was not entered")
        _current_scope.reset(self._token)
        task = asyncio.create_task(self._dispose())
        _keep_background(task, "Runner cleanup")
        deadline = time.monotonic() + self.timeout
        cancelled = None
        while not task.done():
            try:
                await asyncio.wait({task}, timeout=max(0.0, deadline - time.monotonic()))
            except asyncio.CancelledError as cancellation:
                cancelled = cancellation
            if time.monotonic() >= deadline:
                break
        failure: Optional[BaseException] = None
        if task.done():
            failure = asyncio.CancelledError() if task.cancelled() else task.exception()
        else:
            with self._lock:
                names = ", ".join(d.name for d in self.devices.values())
            failure = CleanupError(f"Cleanup deferred beyond {self.timeout:.3g}s: {names}")
        if failure is not None:
            log.error("%s", failure)
        # Preserve the execution failure/cancellation, including repeated cancel.
        if exc is not None:
            return False
        if cancelled is not None:
            raise cancelled
        if failure is not None:
            raise failure
        return False
