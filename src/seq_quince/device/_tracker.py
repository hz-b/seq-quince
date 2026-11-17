import asyncio
from threading import Lock
from typing import Callable, Dict, Optional, Tuple, TypeVar

T = TypeVar("T")


class Tracker:
    """
    track(), dispatch() and wait_all() run on the event loop. 
    commands() and pending() only read (no hardware I/O), and may also be called from executor workers.
    Futures in the tracker: 
        blocking hardware calls, readiness reads, async operations, scheduler and rollback steps, stop requests
    """

    def __init__(self) -> None:
        self._work: Dict[asyncio.Future, bool] = {}  # One asyncio.Future is a command.
        self._lock = Lock()
        # Number of commands tracked so far. A readiness sample is valid only
        # if this did not change while the sample was in flight.
        self.generation = 0

    def track(self, future: asyncio.Future, *, command: bool = False) -> None:
        """Keep the future until it completes; its waiter may be cancelled."""
        with self._lock:
            self._register(future, command)
        future.add_done_callback(self._finished)

    def dispatch(self, function: Callable[..., T], *args, command: bool = False) -> "asyncio.Future[T]":
        """
        Lock and start the provided function(*args) in a worker thread and add it to the tracker.
        """
        with self._lock:
            future = asyncio.get_running_loop().run_in_executor(None, function, *args)
            self._register(future, command)
        future.add_done_callback(self._finished)
        return future

    def _register(self, future: asyncio.Future, command: bool) -> None:
        """ 
        Write this job into the list. If it is a command, also add one to the command counter. 
        Shared by track and dispatch
        """
        if command:
            self.generation += 1
        self._work[future] = command

    def _finished(self, future: asyncio.Future) -> None:
        """
        This job is done. Cross it off
        The generation counter is not lowered here. It only counts how many commands were ever started.
        """
        with self._lock:
            self._work.pop(future, None)
        if not future.cancelled():
            future.exception()  # Observe failures even if its waiter was cancelled.

    def commands(self) -> Tuple[asyncio.Future, ...]:
        """
        All jobs that might move hardware and are still running
        Return: The unfinished commands.
        """
        with self._lock:
            return tuple(
                future for future, command in self._work.items()
                if command and not future.done()
            )

    def pending(self) -> bool:
        """True while any command is outstanding."""
        return bool(self.commands())

    async def wait_all(self, *, exclude: Optional[asyncio.Future] = None) -> None:
        """
        Wait until nothing is tracked, including work that starts meanwhile.
        ``exclude`` is the caller's own task, which cannot wait for itself.
        """
        while True:
            with self._lock:
                work = set(self._work)
            work.discard(exclude)
            if not work:
                return
            await asyncio.wait(work)
            # Done callbacks untrack work and may start more; let them run first.
            await asyncio.sleep(0)
