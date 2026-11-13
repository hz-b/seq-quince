# ToggleDevice

`ToggleDevice` compares a readback PV with a requested state. When they differ,
it writes one configured command value to a toggle PV.

Registry type: `ToggleDevice`  

## Configuration

| Key | Type | Required/default | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required: `ToggleDevice` | Factory type. |
| `name` | string | Required | Device name used in presets. |
| `readback_pv` | string | Required | PV reporting the current state. |
| `setpoint` | float | Required | Default desired readback state. |
| `toggle_pv` | string | Required | PV receiving the toggle command. |
| `toggle_val` | float | `1.0` | Value written when a toggle is needed. |

```yaml
devices:
  - name: diode
    type: ToggleDevice
    readback_pv: "IOC:DIODE:State"
    setpoint: 1
    toggle_pv: "IOC:DIODE:Toggle"
    toggle_val: 1

presets:
  insert_diode:
    diode: {}
  retract_diode:
    diode:
      setpoint: 0
```

## Pose and processing

| Pose key | Default | Meaning |
| --- | --- | --- |
| `setpoint` | Configured `setpoint` | Desired state for this call only. |

The adapter reads the state once and uses exact `!=` comparison. If it differs
from the setpoint, it calls `toggle_pv.put(toggle_val)` once. It does not write
the setpoint itself.

Add a subsequent [WaitConditionDevice](README-wait-condition.md) in a serial
sequence when later operations depend on the new state having been reached.

## State and lifecycle

| Member | Behavior |
| --- | --- |
| `snapshot()` | Returns `{"setpoint": current_readback}`; this mapping can be replayed by `process()`. |
| `busy` | Local flag during the read/compare/write operation. |
| `stop()` | Clears the local flag; it sends no hardware stop command. |
| `get_capability(...)` | No extra capability. |
| `supports_rollback()` | Inherited `True`; replays the saved `setpoint`. |
| `scheduler_timeout()` | Inherited `None`; uses the scheduler's global `step_timeout`. |

