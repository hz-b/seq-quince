# HexapodDevice

`HexapodDevice` configures six kiwi-scan EPICS actuator axes named `Tx`, `Ty`,
`Tz`, `Rx`, `Ry`, and `Rz`. It can write an IOC-specific run-after PV before
dispatching moves.

Registry type: `HexapodDevice`  

## Configuration

| Key | Type | Required/default | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required: `HexapodDevice` | Factory type. |
| `name` | string | Required | Device name used in presets. |
| `config` | mapping | Required for construction | Hexapod settings described below. |
| `config.axes` | mapping | All six named axes required | One kiwi-scan `ActuatorConfig` mapping per axis. |
| `config.enable_coordinated` | boolean | `false` | Dispatch all six axes together; otherwise move one axis or restore a full pose sequentially. |
| `config.run_after_pv` | string | Unset | Optional IOC command PV written before each move. |
| `config.run_after_value` | integer/value | Required and not `None` when a run-after PV is configured | Value written to that PV; `0` is accepted. |

All six axes must be configured even when only one axis will be moved.
This example uses illustrative motor records and shows the uncoordinated mode.

```yaml
devices:
  - name: hexapod
    type: HexapodDevice
    config:
      enable_coordinated: false
      axes:
        Tx:
          type: epics
          pv: "IOC:HEX:Tx.VAL"
          rb_pv: "IOC:HEX:Tx.RBV"
          status_pv: "IOC:HEX:Tx.DMOV"
          ready_value: 1
        Ty:
          type: epics
          pv: "IOC:HEX:Ty.VAL"
          rb_pv: "IOC:HEX:Ty.RBV"
          status_pv: "IOC:HEX:Ty.DMOV"
          ready_value: 1
        Tz:
          type: epics
          pv: "IOC:HEX:Tz.VAL"
          rb_pv: "IOC:HEX:Tz.RBV"
          status_pv: "IOC:HEX:Tz.DMOV"
          ready_value: 1
        Rx:
          type: epics
          pv: "IOC:HEX:Rx.VAL"
          rb_pv: "IOC:HEX:Rx.RBV"
          status_pv: "IOC:HEX:Rx.DMOV"
          ready_value: 1
        Ry:
          type: epics
          pv: "IOC:HEX:Ry.VAL"
          rb_pv: "IOC:HEX:Ry.RBV"
          status_pv: "IOC:HEX:Ry.DMOV"
          ready_value: 1
        Rz:
          type: epics
          pv: "IOC:HEX:Rz.VAL"
          rb_pv: "IOC:HEX:Rz.RBV"
          status_pv: "IOC:HEX:Rz.DMOV"
          ready_value: 1

presets:
  shift_x:
    hexapod:
      Tx: 10.0
```

The axis mappings use the same nested actuator schema as
[ActuatorDevice](README-actuator.md). Additional actuator fields belong inside
each axis mapping. Engineering units come from the configured PVs; the adapter
performs no conversion. 

## Pose modes

| `enable_coordinated` | Accepted pose |
| --- | --- |
| `false` | Exactly one named axis, or exactly all six axes for sequential restoration. |
| `true` | Exactly all six axis keys, with no additional keys. |
Full snapshots can be replayed with `enable_coordinated: false`, including by
scheduler rollback and explicit snapshot loading.
To request the coordinated configuration, set these keys in the existing
hexapod `config` mapping, preserving all six `axes` entries:

```yaml
enable_coordinated: true
run_after_pv: "IOC:HEX:RunAfter"
run_after_value: 1
```

A corresponding preset entry is:

```yaml
presets:
  pose_all_axes:
    hexapod:
      Tx: 10.0
      Ty: 0.0
      Tz: 0.0
      Rx: 0.0
      Ry: 0.0
      Rz: 0.0
```

Coordination depends on the IOC implementation. The adapter does not
calculate a trajectory or guarantee a simultaneous six-axis move. 
The optional `run_after_pv` can be used as coordination command. 

## State and capabilities

| Member | Behavior intended by the adapter |
| --- | --- |
| `process()` | Validate the complete pose, optionally write run-after, then dispatch coordinated moves or sequential restoration with `run_move(target, True)`. |
| `snapshot()` | Read all six axes and return `{Tx, Ty, Tz, Rx, Ry, Rz}`. |
| `busy` | Quick cached readiness state plus outstanding tracked work; initially true until readiness has been sampled. Performs no hardware reads. |
| `stop()` | Await stop requests for every axis and report failures after all attempts. Request completion does not confirm cessation of motion. |
| `get_capability("actuators")` | Return the axis-name-to-`EpicsActuator` dictionary. |
| `supports_rollback()` | Inherited `True`. |
| `scheduler_timeout()` | Inherited `None`; uses the scheduler's global `step_timeout`. |

