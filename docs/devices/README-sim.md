# SimDevice

`SimDevice` simulates an action by waiting for a configurable delay and then
storing the requested values. It accepts arbitrary axis names and performs no
hardware communication.

Registry type: `SimDevice`  

## Configuration

| Key | Type | Required/default | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required: `SimDevice` | Factory type. |
| `name` | string | Required | Device name used in presets. |
| `initial_pose` | mapping | `{"position": 0.0}` | Initial stored values when created through the factory. |
| `delay` | float | `0.01` seconds | Simulated action delay when created through the factory. |

```yaml
devices:
  - name: simulation
    type: SimDevice
    initial_pose:
      x: 0.0
      y: 0.0
    delay: 0.05

presets:
  move_xy:
    simulation:
      x: 1.0
      y: 2.0
```

## State and lifecycle

| Member | Behavior |
| --- | --- |
| `snapshot()` | Returns a shallow copy of the stored pose. |
| `busy` | Local flag set while processing; cleared on success, failure, and cancellation. |
| `stop()` | Clears the flag; it does not cancel an active sleep or update. |
| `get_capability(...)` | No extra capability. |
| `supports_rollback()` | Inherited `True`; replays the saved mapping. |
| `scheduler_timeout()` | Inherited `None`; uses the scheduler's global `step_timeout`. |

