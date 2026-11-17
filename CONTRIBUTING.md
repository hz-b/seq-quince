# Contributing to seq_quince

Seq-quince runs configurable device sequences for beamline operation using asyncio,
EPICS, and kiwi-scan. These guidelines are written for contributors and AI agents.

## Making changes

- Read the relevant implementation, configuration, and tests before editing.
- Keep changes focused. Preserve unrelated work and avoid incidental refactoring.
- Preserve public APIs, YAML keys, defaults, and execution order unless the task
  explicitly changes them. Document intentional behavior changes.
- Keep Python 3.8 compatibility. For example, use `run_in_executor()` instead of
  `asyncio.to_thread()`.
- Comment on context, e.g. reasons or hardware assumptions that the code cannot explain.
- User output can use unicode symbols from docs/misc/symbols.md for better readability.

## Devices and scheduling

- Default device implementations are located in `src/seq_quince/device_concrete/`. Follow
  `DeviceABC` in `src/seq_quince/device/device_abs.py`.
- In-tree devices are registered in `DEVICE_REGISTRY` in `src/seq_quince/device/factory.py`.
- External devcies extending the framework live outside the package: 
  Export it from a module as `DEVICE_CLASSES = {"MyDevice": MyDevice}` and add that directory
  to the `SEQ_QUINCE_DEVICE_PATH` environment variable. Do not move it in-tree before it is stable.  
- Use the lifecycle helpers described in `docs/resource-lifecycle.md`: call `ensure_open(self)`
  at the start of `process()` and `snapshot()`, register owned PVs and drivers with
  `track_resource()`, and run blocking hardware calls through `run_blocking()`. Motion devices
  keep a `MotionState` in `self._motion`, list `MotionBusyMixin` before `DeviceABC` in 
  the constructor and set `_cleanup_tracks_work = True`.
- Keep blocking hardware operations out of the asyncio event loop. Preserve
  cancellation, stop, cleanup, and rollback behavior.
- Rollback contract: the scheduler optionally replays the result of `snapshot()` through `process()`, so
  every snapshot must be a valid pose for the same device. Examples for exceptions are wait-only or monitor-only 
  devices, they return `False` from `supports_rollback()`.
- Validate a whole pose (keys, types, finite values, limits) before the first hardware write,
  so a bad target never leaves a move half-issued.
- Logging: lazy formatting, and restrained output in polling loops.

### New device checklist

- Check the in-tree or external device guidelines below.
- In-tree devices: Implementation in `src/seq_quince/device_concrete/<name>.py`, registered in
  `DEVICE_REGISTRY`.
- Documentation in `docs/devices/README-<name>.md` (configuration keys, pose keys, stop,
  rollback, timeouts), plus a row in the device table of `docs/devices.md` and, if the device
  exposes one, in the capability table.
- Tests in `tests/device_units/test_<name>.py`, based on `DeviceTestCase`. Add the module's
  hardware classes (e.g. `EpicsPV`) to the `boundaries` mapping in
  `tests/device_units/_support.py`, and the type name to
  `test_registry_lists_core_devices_and_returns_a_copy` in `test_device_contract.py`.
- An entry under `[Unreleased]` in `CHANGELOG.md`.

### In-tree or external device?

Add a device **in-tree** (`device_concrete/` + `DEVICE_REGISTRY`) when:

- It is useful beyond a single beamline or experiment setup.
- It needs no dependencies beyond what seq-quince already requires (kiwi-scan, EPICS CA).
- Its behaviour and configuration are stable, and it comes with tests and docs

Keep a device **external** (`DEVICE_CLASSES` + `SEQ_QUINCE_DEVICE_PATH`) when any of these holds:

- It is specific to one site, beamline or experiment, or relies on local PV naming.
- It needs extra, vendor or proprietary libraries.
- It is experimental or its interface is still changing.
- It implements a local workaround that should not become part of the public API.

External devices can be moved to in-tree once they are stable and generally useful.

## Validation

Run:

```bash
make lint && make test
```

### Unit tests

Unit tests are located in `tests/`. Use `unittest.TestCase` via `CoreTestCase`
(`tests/core_units/_support.py`) or `DeviceTestCase` (`tests/device_units/_support.py`).
Do not use real Channel Access: patch the driver boundary (e.g. `EpicsPV`, `EpicsActuator`)
in the device module. `DeviceTestCase` makes any unpatched hardware access fail with
`UnexpectedHardwareAccess`.

## Notes

- Code must run on Python 3.8: no `match`, no `list[int]` at runtime, no parenthesized context managers, no `str.removeprefix`.
  Check both syntax and standard-library availability.
- Keep CLI layers thin. Put reusable behavior in library functions and reuse existing configuration models, readers, and formatters.
- Verify the change. Add focused tests. 
- Validate configuration before the run starts. 
