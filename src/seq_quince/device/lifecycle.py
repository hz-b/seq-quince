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
from contextlib import asynccontextmanager
from contextvars import ContextVar, copy_context
from threading import RLock
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
)

from seq_quince.device._tracker import Tracker

log = logging.getLogger(__name__)
T = TypeVar("T")
_POLL_INTERVAL = 0.05  # Seconds between readiness samples while a device is busy.


class CleanupError(RuntimeError):
    """One or more devices could not be disposed within the cleanup budget."""


class _Resource:
    """
    For one device entry of what has to be closed, how to close it, and whether closing has already started
    """
    def __init__(self, resource, cleanup):
        self.resource = resource
        self.cleanup = cleanup
        self.future: Optional[asyncio.Future] = None  # None means nobody has started closing this yet


class _State:
    def __init__(self):
        # The one record of this device's outstanding work.
        self.tracker = Tracker()
        self.resources: Dict[int, _Resource] = {}
        self.lock = RLock()
        self.close_task: Optional[asyncio.Task] = None
        self.closing = False
        self.closed = False
        self.unconfirmed = False
        self.hold: Optional[str] = None


class LifecycleContext:
    """
    The lifecycle bookkeeping that is not owned by a single device: devices
    awaiting safe disposal, cleanup tasks that outlive their callers, the
    active DeviceScope, and where each device's lifecycle state is kept.

    One default instance serves the whole process. A future session can
    install its own with set_lifecycle_context() before it builds devices, and
    override state_for(), for example to give every device object that drives
    the same physical axis one shared state and therefore one tracker.
    """

    def __init__(self) -> None:
        """  Init process-wide notebook for everything that doesn’t belong to a single device """
        self.retained: Dict[int, Any] = {}  # Devices awaiting safe disposal.
        self.background: Set[asyncio.Task] = set()  # Cleanup outliving its caller.
        self.scope: ContextVar = ContextVar("device_scope", default=None) # active DeviceScope 

    def state_for(self, owner) -> _State:
        """
        The lifecycle state of a device; by default one per device object.
        (the default gives one record per device object but future "session" 
        can subclass LifecycleContext and return the same _State for every device object 
        that drives the same physical axis.
        """
        # Resource registration may also happen in an executor worker. Initial
        # registration must precede dispatch; subsequent accesses reuse this state.
        state = getattr(owner, "_resource_lifecycle", None)
        if state is None:
            state = _State()
            owner._resource_lifecycle = state
        return state

    def keep_background(self, task: asyncio.Task, description: str) -> None:
        """
        Hold on to this cleanup task until it ends, then tell the log how it ended
        """
        self.background.add(task)  

        def done(finished): # asyncio calls done(task) by itself passing the future or task that finished
            self.background.discard(finished)
            if finished.cancelled():
                log.error("%s interrupted; retained resources may need manual disposal", description)
            elif finished.exception() is not None:
                log.error("%s failed: %s", description, finished.exception())

        task.add_done_callback(done)

# one default notebook for the whole process created when this module is imported
_context = LifecycleContext()


def lifecycle_context() -> LifecycleContext:
    """The lifecycle context in use."""
    return _context


def set_lifecycle_context(context: LifecycleContext) -> LifecycleContext:
    """
    Install another lifecycle context and return the previous one. Do this
    before devices are built: a device keeps the state it was given, and a
    DeviceScope keeps the context it was created in.
    """
    global _context  
    previous, _context = _context, context
    return previous


def _state(owner) -> _State:
    return _context.state_for(owner)


def ensure_open(owner) -> None:
    """Reject use after successful disposal; close does not reopen devices."""
    if _state(owner).closed:
        raise RuntimeError(f"Device {owner.name!r} is closed")
    if _state(owner).closing:
        raise RuntimeError(f"Device {owner.name!r} is closing")


def own_device(device) -> None:
    """Register a constructed device with the active runner scope, if any."""
    scope = _context.scope.get()
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
    _state(owner).tracker.track(future)


async def run_blocking(owner, function: Callable[..., T], *args) -> T:
    """
    Remember work that is still running:
    Keep the executor future alive when its coroutine waiter is cancelled.
    """
    # The state exists before the worker can register resources, and the worker
    # is pending work before it can read the device's busy state.
    future = _state(owner).tracker.dispatch(function, *args, command=True)
    return await asyncio.shield(future)


def _run_probe(owner, checker: Callable[[], T]) -> "asyncio.Future[T]":
    """
    Read readiness outside the loop. Disposal still waits for the probe, but it
    is not a command: an overlapping probe never invalidates a sample.
    """
    return _state(owner).tracker.dispatch(checker)


async def run_operation(owner, operation: Awaitable[T]) -> T:
    """Track an async operation without abandoning it on waiter cancellation."""
    task = asyncio.ensure_future(operation)
    # Unknown work is a command: it may move hardware outside run_blocking().
    _state(owner).tracker.track(task, command=True)
    return await asyncio.shield(task)


def has_pending_work(owner) -> bool:
    """
    Check if work or motion remains, no hardware operation, non blocking.
    """
    return _state(owner).tracker.pending()


class MotionState:
    """
    Sample blocking readiness outside the asyncioc event loop and expose a conservative cache.
    
    A command sets busy = True and is tracked; 
    the observer keeps sampling through checker; 
    only if:
    - the hardware says ready, 
    - no command was running 
    - none started during the sample 
    it writes busy = False and answers the waiting _requests.
    """

    def __init__(self, owner, checker: Callable[[], bool]):
        """ 
        Init the readiness helper of one motion device (e.g. actuator, ophyd motor, hexapod). 
        It remembers whether the device is busy and runs the a background task that checks the hardware.
        """
        self.owner = owner
        self.checker = checker
        self.busy = True  # Unknown until an async sample confirms readiness.
        self._observer: Optional[asyncio.Task] = None
        self._requests: List[asyncio.Future] = []  # refresh() calls awaiting a sample.
        self._wake: Optional[asyncio.Future] = None  # Ends the observer's pause early.

    async def refresh(self) -> None:
        """
        Wait for a readiness sample that starts after this call, so that it
        cannot predate the command it follows. Sampling errors are raised here.
        """
        request = asyncio.get_running_loop().create_future()
        self._requests.append(request)
        self.watch()
        if self._wake is not None and not self._wake.done():
            self._wake.set_result(None)
        try:
            await request
        except BaseException:
            self.watch()
            raise

    @asynccontextmanager
    async def commands(self) -> AsyncIterator[None]:
        """
        Group tracked hardware commands and confirm readiness once afterwards::

            async with self._motion.commands():
                await run_blocking(self, first_axis.run_move, target, True)
                await run_blocking(self, second_axis.run_move, target, True)

        The device is busy from here until a sample confirms idle, also in the
        moment between the last command and that sample. If the block fails or
        its waiter is cancelled, an observer takes over: neither proves that
        the hardware has stopped.
        """
        self.busy = True
        try:
            yield
        except BaseException:
            self.watch()
            raise
        await self.refresh()

    async def command(self, function: Callable[..., T], *args) -> T:
        """Run one blocking hardware command in a worker, see commands()."""
        async with self.commands():
            return await run_blocking(self.owner, function, *args)

    def watch(self) -> None:
        """Observe until idle: after refresh(), process cancellation, failure, or stop."""
        self.busy = True
        if self._observer is None or self._observer.done():
            self._observer = asyncio.create_task(self._observe())
            _state(self.owner).tracker.track(self._observer)

    async def _observe(self) -> None:
        """The only sampler: answer refresh() calls and poll until idle."""
        tracker = _state(self.owner).tracker
        requests: List[asyncio.Future] = []
        try:
            while True:
                # A sample answers the refresh() calls made before it started.
                requests, self._requests = self._requests, []
                busy, error = await self._sample(tracker)
                if error is not None:
                    if all(request.done() for request in requests):
                        raise error  # Nobody is waiting for it: the device is unconfirmed.
                    # refresh() reports the error; observation continues regardless.
                    _fail(requests, error)
                    continue
                # Validated and published without a suspension point in between.
                self.busy = busy
                for request in requests:
                    if not request.done():
                        request.set_result(busy)
                if self._requests:
                    continue
                if not busy:
                    return
                await self._pause(tracker)
        except BaseException as failure:
            self.busy = True
            mark_unconfirmed(self.owner, "busy state unavailable", hold=True)
            # No sample will answer the refresh() calls that are still waiting.
            requests, self._requests = requests + self._requests, []
            _fail(requests, failure)
            raise

    async def _sample(self, tracker: Tracker) -> Tuple[bool, Optional[BaseException]]:
        """
        Ask the hardware once whether it is busy. Return (busy, error).

        "idle":  
        - only if the hardware says idle
        - no command was running when asked
        - no new command started while waiting for the answer. 
        Otherwise the answer may already be out of date, so say "busy" and ask again later.

        If the hardware read fails, return the error instead of raising it. 
        The refresh() callers would raise it. 
        Raising it here would tie the error to this running observer, 
        and on Python before 3.13 a test could then stop the observer by accident.
        """
        generation = tracker.generation
        pending = tracker.pending()
        probe = _run_probe(self.owner, self.checker)
        await asyncio.wait({probe})  # Waits for the worker even if it fails.
        error = probe.exception()
        if error is not None:
            return True, error
        busy = bool(probe.result())
        return busy or pending or generation != tracker.generation, None

    async def _pause(self, tracker: Tracker) -> None:
        """
        Wait one polling interval. A refresh() call or the completion of a
        pending command ends it early: either can change the answer.
        """
        self._wake = asyncio.get_running_loop().create_future()
        try:
            await asyncio.wait(
                {self._wake, *tracker.commands()},
                timeout=_POLL_INTERVAL,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            self._wake = None


def _fail(requests: List[asyncio.Future], error: BaseException) -> None:
    """End waiting refresh() calls with the error that prevented their sample."""
    for request in requests:
        if request.done():
            continue
        if isinstance(error, asyncio.CancelledError):
            request.cancel()
        else:
            request.set_exception(error)


class MotionBusyMixin:
    """
    The one definition of ``busy`` for adapters that keep their MotionState in
    ``self._motion``: busy until a sample confirmed idle hardware, and while any
    tracked command is outstanding. Reading it never touches the hardware.

    List it before DeviceABC, so that it implements the abstract property.
    """

    _motion: MotionState

    @property
    def busy(self) -> bool:
        return self._motion.busy or has_pending_work(self)

def mark_unconfirmed(owner, reason: str, *, hold: bool = False) -> None:
    """
    Warn disposal that this device may not be done. 
    The scheduler's own stop and rollback are not changed.

    Without ``hold``, the device is closed only once its busy state reads idle.
    With ``hold``, it is not closed at all and stays retained, because we
    cannot know whether it is still moving: its stop failed or did not
    finish, or its busy state cannot be read.

    A class that does not set ``_cleanup_tracks_work = True`` is always held:
    it may run work the tracker does not know about.
    """
    state = _state(owner)
    state.unconfirmed = True
    if hold or not type(owner).__dict__.get("_cleanup_tracks_work", False):
        state.hold = reason


def retained_devices() -> Tuple[Any, ...]:
    """Devices retained for outstanding work, cleanup errors, or uncertainty."""
    return tuple(_context.retained.values())

async def _settle(owner, state, *, exclude_current=False) -> None:
    """
    Wait until it is safe to close this device's drivers, or raise
    CleanupError if it never will be.

    First wait for all work the tracker knows about. Then:
    - held device: raise, nothing gets closed;
    - unconfirmed device: ask busy every 50 ms until it says idle. If busy
      cannot be read, hold the device and raise.

    With exclude_current, do not wait for the task that is calling us:
    if it is tracked itself, it would wait for itself forever.
    """
    current = asyncio.current_task() if exclude_current else None
    await state.tracker.wait_all(exclude=current)   # 1. tracked work done
    if state.hold:                                  # 2. hard sticker: keep open
        raise CleanupError(f"{owner.name} retained: {state.hold}")
    if state.unconfirmed:                           # 3. soft sticker: prove idle
        # An empty tracker is not enough here: the hardware may still move.
        while True:
            try:
                # Read busy in a worker thread; this read is not a command.
                busy = await asyncio.shield(_run_probe(owner, lambda: owner.busy))
            except Exception as exc:
                state.hold = "busy state unavailable"   # cannot know: keep open
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
    retained = _context.retained
    retained[id(owner)] = owner
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
        retained.pop(id(owner), None)


async def close_resources(owner) -> None:
    """Idempotent, shielded disposal of resources registered by an adapter."""
    state = _state(owner)
    state.closing = True
    task = state.close_task
    if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
        task = asyncio.create_task(_close_resources(owner, state))
        state.close_task = task
        _context.keep_background(task, "Device cleanup")
    await asyncio.shield(task)


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
        self._construction = Tracker()
        self._context = _context
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

        return await asyncio.shield(self._construction.dispatch(invoke))
    
    # Automatically called for: async with DeviceScope() as scope: ...
    # Save the token so __aexit__() can restore the previous scope.
    async def __aenter__(self):
        self._token = self._context.scope.set(self)
        return self

    async def _dispose(self) -> None:
        await self._construction.wait_all()
        with self._lock:
            devices = tuple(self.devices.values())
        retained = self._context.retained

        async def close(device):
            state = self._context.state_for(device)
            retained[id(device)] = device
            try:
                # Not left to device.close(): an extension may override it, and
                # must not release hardware that is held or still working.
                await _settle(device, state)
                await device.close()
            except BaseException:
                log.exception("Device cleanup failed for %s", device.name)
                raise
            else:
                state.closed = True
                retained.pop(id(device), None)

        results = await asyncio.gather(*(close(d) for d in devices), return_exceptions=True)
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            names = ", ".join(d.name for d in devices if id(d) in retained)
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
        self._context.scope.reset(self._token)
        task = asyncio.create_task(self._dispose())
        self._context.keep_background(task, "Runner cleanup")
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
