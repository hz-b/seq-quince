# Device resource lifecycle

The lifecycle API provides await device.close() for repeatable resource cleanup and async with DeviceScope(): for automatic ownership and cleanup of registered devices. Adapters use track_resource() to register owned resources and their cleanup functions, run_blocking() to track executor work, and run_operation() to track asynchronous work through cancellation. retained_devices() lists devices awaiting safe disposal; CleanupError reports cleanup failures when no execution error already takes priority.

Each device has one tracker for its outstanding work. Every tracked future is kept until it completes, also after its caller was cancelled, and disposal waits for all of them. Worker-thread work is registered before its worker can read the tracker, so a command always finds its own device busy. A role says what else it means:

| Role | Tracked by | Meaning |
| --- | --- | --- |
| Command | `run_blocking()`, `run_operation()` | May change hardware. Counts as pending work in `has_pending_work()`, and a readiness sample taken around it cannot confirm idle. |
| Probe | Readiness reads | Only reads. Never pending work. |
| Background | `track_task()`, the readiness observer | Supervising work such as a scheduler step. Commands it issues are tracked on their own. |

Motion adapters keep a `MotionState` in `self._motion`. They issue one blocking hardware command with `await self._motion.command(function, *args)`, or group several `run_blocking()` calls in `async with self._motion.commands():`. Both sample readiness once afterwards, and leave the device busy and observed when a command fails or its caller is cancelled. `MotionBusyMixin`, listed before `DeviceABC`, defines `busy` once for these adapters: cached motion state plus outstanding tracked work, without hardware reads.

One observer task per `MotionState` takes every readiness sample and is the only writer of an idle result. `refresh()` asks it for a sample that starts after the call and waits for the answer, so a sample can never predate the command it follows. The observer polls every 50 ms while the device is busy, samples at once when `refresh()` is called or a pending command completes, and ends when idle is confirmed. A sampling error is raised by the waiting `refresh()`; if nobody is waiting, the device is retained as unconfirmed.

Bookkeeping that is not owned by a single device lives in one `LifecycleContext`: the devices in `retained_devices()`, cleanup tasks that outlive their callers, the active `DeviceScope`, and the lookup of each device's lifecycle state. One default context serves the whole process; `lifecycle_context()` returns the one in use. A session can install its own with `set_lifecycle_context()` before it builds devices, and override `state_for()` to give every device object that drives the same physical axis one shared state, and therefore one tracker and one busy state. Shared state also means shared ownership: closing one of those device objects closes the axis for all of them, until borrowing without closing is possible.
