# Built-in device types

Devices are listed under `devices:` in `beamline.yaml`. Every entry needs `name` and `type`; the remaining keys depend on
the type. Types are resolved by `seq_quince.device.factory.DeviceFactory` (`DEVICE_REGISTRY`). Custom types can be added with
`DeviceFactory.register("MyDevice", MyDeviceClass)`.

| `type`                   | Module                                   | Config keys                                                                 | Pose keys (preset)                     | Rollback |
|--------------------------|------------------------------------------|-----------------------------------------------------------------------------|----------------------------------------|----------|
| [SimDevice](https://github.com/hz-b/seq-quince/blob/master/docs/devices/README-sim.md) | `device_concrete/sim.py`       | `delay`, `initial_pose`                                                     | any                                    | yes      |
| [OphydMotorDevice](https://github.com/hz-b/seq-quince/blob/master/docs/devices/README-ophyd-motor.md) | `device_concrete/ophyd_motor.py` | `pv_prefix`                                                | `position`                             | yes      |
| [ActuatorDevice](https://github.com/hz-b/seq-quince/blob/master/docs/devices/README-actuator.md) | `device_concrete/actuator.py` | `config` (kiwi-scan `ActuatorConfig`)                              | `position`                             | yes      |
| [HexapodDevice](https://github.com/hz-b/seq-quince/blob/master/docs/devices/README-hexapod.md) | `device_concrete/hexapod.py` | `config.axes.{Tx,Ty,Tz,Rx,Ry,Rz}` (ActuatorConfig each), `config.enable_coordinated`, `config.run_after_pv`, `config.run_after_value` | `Tx`…`Rz` (all when coordinated, else exactly one) | yes |
| [SequenceDevice](https://github.com/hz-b/seq-quince/blob/master/docs/devices/README-sequence.md) | `device_concrete/sequence.py` | `sequence.<step>.{pv, value, queue_delay, desired_pv}`             | `<step>` value overrides               | yes      |
| [ToggleDevice](https://github.com/hz-b/seq-quince/blob/master/docs/devices/README-toggle.md) | `device_concrete/toggle.py` | `readback_pv`, `setpoint`, `toggle_pv`, `toggle_val`                     | `setpoint`                             | yes      |
| [ScanDevice](https://github.com/hz-b/seq-quince/blob/master/docs/devices/README-scan.md) | `device_concrete/scan.py`    | `scantype`, `config`, `replacements`, `data_dir`, `actuator`, `start`, `stop`, `steps`, `velocity` | `start`, `stop`, `steps`, `velocity` | no (acquisition) |
| [MonoZeroOrderDevice](https://github.com/hz-b/seq-quince/blob/master/docs/devices/README-mono-zero-order.md) | `device_concrete/mono_zero_order.py` | `alpha_pv`, `beta_pv`, `theta_pv`, `watchdog_pv`, `pause_pv`, `mode` (`stddev`/`spread`/`reference`), `in_position_band`, `poll_period`, `stable_reads`, `timeout`, `log_every`, `pv_read_timeout`, `read_retry_count`, `max_consecutive_read_failures`, `reference` | same as config keys (top level or under `options`) | no (wait-only) |
| [WaitConditionDevice](https://github.com/hz-b/seq-quince/blob/master/docs/devices/README-wait-condition.md) | `device_concrete/wait_condition.py` | `pv`, `value`, `condition` (`eq`, `ne`, `gt`, `ge`, `lt`, `le` and aliases), `poll_period`, `timeout`, `pv_read_timeout`, `read_retry_count`, `max_consecutive_read_failures`, `log_every`, `watchdog_pv`, `watchdog_frequency` | same as config keys (top level or under `options`) | no (wait-only) |

## Capabilities

`device.get_capability(name)` exposes device-specific objects:

| Device                   | Capability name           | Object                          |
|--------------------------|---------------------------|---------------------------------|
| `ActuatorDevice`         | `actuator`                | kiwi-scan `EpicsActuator`       |
| `HexapodDevice`          | `actuators`               | dict of axis → `EpicsActuator`  |
| `ScanDevice`             | `scan`                    | kiwi-scan scan instance         |

## Scheduling

`build_sequence(devices, preset, default_grouping=...)` turns a preset into `MoveStep`s:

- `serial` – one group per device, executed in preset order (used by `scheduler_runner`).
- `parallel` – all devices in group 0, followed by a barrier.

`Scheduler.run()` captures a snapshot of each rollback-capable device before its group runs. If any step fails, completed
steps are driven back to those snapshots (best effort) and `MoveError` is raised. Per-step timeouts come from
`device.scheduler_timeout(pose)`, falling back to the scheduler's `step_timeout`.

`ScanDevice` does not support automatic rollback. 
Completed scans are not rerun during recovery. Active scans still receive stop
requests during failure recovery or cancellation.

## Scheduler device API

Scheduler devices expose hardware actions, scans, and wait conditions through
the common `DeviceABC` interface. 

### DeviceABC interface

| Member | Use |
| --- | --- |
| `from_config_dict(config)` | Create a device from its YAML mapping. |
| `await process(pose)` | Perform one action or wait. |
| `await snapshot()` | Return readbacks or diagnostic/configuration values. |
| `busy` | Report adapter-specific activity or hardware state. |
| `await stop()` | Request the adapter's implemented stop action. |
| `get_capability(name)` | Access an optional underlying driver or command API. |
| `supports_rollback()` | Tell the scheduler whether to capture and replay snapshots. |
| `scheduler_timeout(pose)` | Supply a timeout override; `None` inherits the global `step_timeout`. |

## Configuration structure

Each `devices` list entry defines an instance. A preset maps each device name to
its runtime **pose**: the values or options passed to `process()`.

```yaml
devices:
  - name: simulation
    type: SimDevice
    initial_pose:
      position: 0.0
    delay: 0.05

presets:
  demo:
    simulation:
      position: 10.0
```

The configuration manager normally loads `beamline.yaml`, merges presets from
`presets.yaml` with the saved presets taking precedence, and renders the main
file through Jinja2. The loader accepts `${TOKEN}` and `{{ TOKEN }}` placeholders
using the supplied replacements. Device-level `replacements` are used by
`ScanDevice`; they are not a general PV substitution mechanism in each device.


## Resource ownership

See [Device resource lifecycle](https://github.com/hz-b/seq-quince/blob/master/docs/resource-lifecycle.md) for stop versus close,
runner ownership, cancellation, deferred disposal, and extension requirements.
