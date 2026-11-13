# SequenceDevice

`SequenceDevice` performs an ordered list of EPICS PV writes. Each logical entry
can obtain its value from the runtime pose, a separate desired-value PV, or a
configured constant.

Registry type: `SequenceDevice`  

## Configuration

| Key | Type | Required/default | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required: `SequenceDevice` | Factory type. |
| `name` | string | Required | Device name used in presets. |
| `sequence` | mapping | `{}` | Logical names mapped to PV-write definitions, executed in insertion order. |

Each entry in `sequence` accepts:

| Entry key | Type | Required/default | Meaning |
| --- | --- | --- | --- |
| `pv` | string | Needed to perform the write | Destination PV. |
| `value` | any | No default | Constant fallback when no pose override or `desired_pv` is supplied. |
| `desired_pv` | string | Unset | PV read at processing time to obtain the value. |
| `queue_delay` | float | `0.01` seconds | Passed to the destination `EpicsPV` as `queueing_delay` and also used for an explicit sleep after the write. |

```yaml
devices:
  - name: configure_scan
    type: SequenceDevice
    sequence:
      Harmonic:
        pv: "IOC:ID:Harmonic"
        value: 3
        queue_delay: 0.1
      Energy:
        pv: "IOC:MONO:SetEnergy"
        desired_pv: "IOC:MONO:DesiredEnergy"
        queue_delay: 0.5
      Enable:
        pv: "IOC:ID:Enable"
        value: 1

presets:
  configure:
    configure_scan:
      Harmonic: 5
```

The example writes `Harmonic=5`, then reads the desired-energy PV and writes that
value, then writes `Enable=1`. A pose `{}` uses all configured value sources.

## Value selection and processing

For each configured logical name, value selection is:

1. A pose value that is not `None`.
2. A read from `desired_pv`, when configured.
3. The configured `value`, when no `desired_pv` is configured.

## State and lifecycle

| Member | Behavior |
| --- | --- |
| `snapshot()` | Reads initialized destination PVs and returns `{logical_name: value}`. |
| `busy` | Local flag while the write sequence is active. |
| `stop()` | Sets an abort event and waits up to about two seconds for acknowledgement. |
| `get_capability(...)` | No extra capability. |
| `supports_rollback()` | Inherited `True`; replays the saved destination values in configured order. |
| `scheduler_timeout()` | Inherited `None`; uses the scheduler's global `step_timeout`. |


