# OphydMotorDevice

`OphydMotorDevice` wraps one EPICS motor record using `ophyd.EpicsMotor`.

Registry type: `OphydMotorDevice`  

## Configuration

| Key | Type | Required/default | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required: `OphydMotorDevice` | Factory type. |
| `name` | string | Required | Scheduler device name and ophyd object name. |
| `pv_prefix` | string | Required | EPICS motor record prefix. |

```yaml
devices:
  - name: motor_record
    type: OphydMotorDevice
    pv_prefix: "IOC:MOTOR:m1"

presets:
  move_motor:
    motor_record:
      position: 10.0
```

## Pose and processing

| Pose key | Required/default | Meaning |
| --- | --- | --- |
| `position` | Required, must not be `None` | Absolute target. |


## State and lifecycle

| Member | Behavior |
| --- | --- |
| `snapshot()` | Returns `{"position": motor.position}`. |
| `busy` | Cached motion state plus pending worker activity; initially true until sampled. Performs no hardware reads. |
| `stop()` | Awaits `motor.stop()` and samples readiness in workers. Exceptions propagate; request completion does not confirm cessation of motion. |
| `get_capability(...)` | No named capability; the underlying object is available as `device.motor`. |
| `supports_rollback()` | Inherited `True`; attempts another position move. |
| `scheduler_timeout()` | Inherited `None`; uses the scheduler's global `step_timeout`. |

See [resource lifecycle](../resource-lifecycle.md) for cancellation and cleanup.
