# Run one device preset

Run only one configured device's entry from a named preset:

```bash
device_runner --config my_beamline --device mono --preset alignment
device_runner --config my_beamline --device mono --preset alignment --replace IOC=TEST
```

The configuration name and directory resolution match `scheduler_runner`:
`SCHEDULER_CONFIG_DIR` overrides the directory, otherwise the usual
`SEQ_QUINCE_CONFIG_DIR` or packaged beamline directory is used. The files are
`beamline.yaml` and optional `presets.yaml`; saved presets retain their normal
precedence. `--config` takes the beamline name, as in the existing CLI.

For example, given this `beamline.yaml`:

```yaml
devices:
  - name: mono
    type: SimDevice
    initial_pose: {position: 1.0}
    delay: 0
  - name: slit
    type: SimDevice
    initial_pose: {position: 0.0}
    delay: 0
presets:
  alignment:
    mono: {position: 5.0}
    slit: {position: 2.0}
```

The example command constructs `mono` and runs it with `{position: 5.0}`.
It does not construct `slit`. Selecting a composite device, such as
`SequenceDevice`, still executes that device's configured internal operations.
Empty entries (`mono: {}`) are accepted for devices that use configured defaults.

The selection is validated before the factory constructs the device. An
unknown preset or device, a duplicate device name, a missing device entry,
or an entry that is not a mapping causes an error. Selected config and pose
data are copied before use, preserving the loaded manager's configuration.

The existing scheduler executes one step and applies the device's scheduler
timeout. Device implementations retain their normal processing and error
cleanup. The scheduler rolls back completed steps; a single failing step
has no completed step to roll back. Cancelling the async operation attempts
`device.stop()` before propagating cancellation. A stop failure is logged
without hiding cancellation. Ctrl-C uses this cancellation path during
execution and exits with status 130; a repeated interrupt can abort cleanup.

| Option | Meaning |
| --- | --- |
| `--config NAME` | Required beamline configuration name |
| `--device NAME` | Required configured device name |
| `--preset NAME` | Required named preset containing that device |
| `--replace KEY=VALUE ...` | Standard configuration replacements |
| `--log-level 0-5` | Same logging control as `scheduler_runner` |

Exit codes: 0 for success, 1 for configuration or execution failure, 2 for
argument errors, and 130 for interruption. Execution runs once.

## Python use

```python
from seq_quince.runner import run_device

run_device("my_beamline", "mono", "alignment", replacements={"IOC": "TEST"})
```

For an application that already owns an event loop:

```python
from seq_quince.runner import create_config_manager, run_device_preset

async def align_mono():
    manager = create_config_manager("my_beamline", {"IOC": "TEST"})
    await run_device_preset(manager, "mono", "alignment")
```

`run_device()` owns an event loop; use the async helper inside a running loop.
Device construction happens inside the execution loop, including on Python 3.8.
`create_device_step()` is also available for applications building their own
scheduler steps; call it inside the loop used to execute the device.
