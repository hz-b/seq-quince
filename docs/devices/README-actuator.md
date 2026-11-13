# ActuatorDevice

`ActuatorDevice` wraps one kiwi-scan `EpicsActuator` for position moves.

Registry type: `ActuatorDevice`  

## Configuration

| Key | Type | Required/default | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required: `ActuatorDevice` | Factory type. |
| `name` | string | Required | Device name used in presets. |
| `config` | mapping | Required for construction | One kiwi-scan [ActuatorConfig](https://github.com/hz-b/kiwi-scan/blob/master/docs/actuator.md) mapping. |

The adapter passes the nested mapping directly to `ActuatorConfig.from_dict()`
and always constructs `EpicsActuator`. The nested `type` describes the actuator
configuration.

```yaml
devices:
  - name: mono_energy
    type: ActuatorDevice
    config:
      type: epics
      pv: "IOC:MONO:SetEnergy"
      rb_pv: "IOC:MONO:GetEnergy"
      status_pv: "IOC:MONO:State"
      ready_value: 0
      stop_pv: "IOC:MONO:Stop"
      stop_command: 1
      in_position_band: 0.01

presets:
  set_energy:
    mono_energy:
      position: 350.0
```

The inner PV fields follow the upstream
[kiwi-scan actuator reference](https://github.com/hz-b/kiwi-scan/blob/master/docs/actuator.md).
Use the full `ActuatorConfig` schema supported by the installed kiwi-scan
version for optional velocity, startup, dwell, and other actuator settings.

## Pose and processing

| Pose key | Required/default | Meaning |
| --- | --- | --- |
| `position` | Required, must not be `None` | Absolute target in the actuator's engineering units. |

`process()` calls the blocking `run_move(position, True)` in a worker thread and waits for
the call to return. 

## State and capabilities

| Member | Behavior |
| --- | --- |
| `snapshot()` | Returns `{"position": actuator.rbv}`. |
| `busy` | Cached readiness plus pending worker activity; initially true until sampled. Performs no hardware reads. |
| `stop()` | Awaits the underlying stop request and samples readiness in workers; driver exceptions propagate. Motion may continue after the request returns. |
| `get_capability("actuator")` | Returns the underlying `EpicsActuator`. |
| `supports_rollback()` | Inherited `True`; replay moves to the saved position. |
| `scheduler_timeout()` | Inherited `None`; uses the scheduler's global `step_timeout`. |

Application code can inspect the driver through the capability, for example:

```python
actuator = devices["mono_energy"].get_capability("actuator")
readback = actuator.rbv
ready = actuator.is_ready()
```
See [resource lifecycle](../resource-lifecycle.md) for cancellation and cleanup.
