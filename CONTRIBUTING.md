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
- User output can use unicode symbols from doc/misc/symbols.md for better readability

## Devices and scheduling

- Default device implementations are located in `src/seq_quince/device_concrete/`. Follow
  `DeviceABC` in `src/seq_quince/device/device_abs.py` and register new device types
  through `DeviceFactory` in `src/seq_quince/device/factory.py`.
- New devices are usually tested as external device, register them with DEVICE_CLASSES and 
  set the SEQ_QUINCE_DEVICE_PATH environment variable.
- Keep blocking hardware operations out of the asyncio event loop. Preserve
  cancellation, stop, cleanup, and rollback behavior.
- Logging: lazy formatting, and restrained output in polling loops.

## Validation

Run:

```bash
make lint && make test
```

### Unit tests

Unittests are located in tests.  Use unittest.TestCase (CoreTestCase or DeviceTestCase)
Do not use real CA.

## Notes

- Code must run on Python 3.8: no `match`, no `list[int]` at runtime, no parenthesized context managers, no `str.removeprefix`.
  Check both syntax and standard-library availability.
- Keep CLI layers thin. Put reusable behavior in library functions and reuse existing configuration models, readers, and formatters.
- Verify the change. Add focused tests. 
- Validate configuration before the run starts. 
