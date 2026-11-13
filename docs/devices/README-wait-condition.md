# WaitConditionDevice

`WaitConditionDevice` waits until a PV value satisfies a comparison. It issues
no motion command and can optionally reset a watchdog while waiting.

Registry type: `WaitConditionDevice`  

## Configuration

All keys below except `type`, `name`, and `options` may be configured at device
level or inside `options`. Device-level keys take precedence.

| Key | Type | Required/default | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required: `WaitConditionDevice` | Factory type. |
| `name` | string | Required | Device name used in presets. |
| `options` | mapping | `{}` | Alternate location for wait settings. |
| `pv` | string | Required by processing; may be supplied in the pose | PV to read. |
| `value` | any | Required by processing; must not be `None` | Expected comparison value. |
| `condition` | string | `eq` | Comparison name or alias. |
| `poll_period` | float | `0.2` seconds | Positive delay between samples or failed sampling rounds. |
| `timeout` | float | `0.0` seconds | Internal wait timeout; zero disables it, negative values are rejected. |
| `pv_read_timeout` | float | `0.5` seconds | Positive timeout passed to `EpicsPV.get()`. |
| `read_retry_count` | integer | `3` | Read attempts per sampling round. |
| `max_consecutive_read_failures` | integer | `5` | Failed sampling rounds before a fatal error. |
| `log_every` | integer | `10` | INFO sample-log interval, in addition to the first and satisfying samples. |
| `watchdog_pv` | string | Unset | Optional PV to reset to `0`. |
| `watchdog_frequency` | float | Unset | Positive watchdog reset frequency in Hz. |
| `frequency` | float | Alias of `watchdog_frequency` | Canonical name wins over the alias at the same level. |

```yaml
devices:
  - name: wait_ready
    type: WaitConditionDevice
    pv: "IOC:MONO:State"
    value: 0
    condition: eq
    options:
      poll_period: 0.2
      timeout: 30.0
      pv_read_timeout: 0.5
      read_retry_count: 3
      max_consecutive_read_failures: 5
      log_every: 10

presets:
  ready:
    wait_ready: {}
```

An instance may be configured with only `name` and `type` and receive its `pv`
and `value` through every pose.

## Supported comparisons

| Canonical name | Operation | Accepted aliases |
| --- | --- | --- |
| `eq` | Equal | `==`, `equals`, `equal` |
| `ne` | Not equal | `!=`, `not_equal`, `not_equals` |
| `gt` | Greater than | `>`, `greater`, `greater_than` |
| `ge` | Greater than or equal | `>=`, `gte`, `greater_equal`, `greater_or_equal` |
| `lt` | Less than | `<`, `less`, `less_than` |
| `le` | Less than or equal | `<=`, `lte`, `less_equal`, `less_or_equal` |

Quote symbolic operators in YAML, for example `condition: ">="`.
Ordered comparisons convert both operands to floats. Equality and inequality
also try numeric conversion first; if either fails, strings are stripped and
the original scalar values are compared. Thus numeric strings can equal
numbers, while text comparison remains case-sensitive. No numerical tolerance,
stability count, array comparison, or finite-number validation is added.

## Pose overrides

Every wait setting in the configuration table can be overridden by a pose at
top level or inside `pose.options`.

```yaml
presets:
  wait_temperature:
    wait_ready:
      pv: "IOC:TEMP:Readback"
      value: 280.0
      condition: ">="
      options:
        timeout: 120.0
        poll_period: 0.5
```

## Watchdog

To enable watchdog resets, supply both the PV and a positive frequency:

```yaml
devices:
  - name: wait_ready_watchdog
    type: WaitConditionDevice
    pv: "IOC:MONO:State"
    value: 0
    condition: eq
    watchdog_pv: "IOC:MONO:Watchdog"
    watchdog_frequency: 5.0
    timeout: 30.0

presets:
  ready_with_watchdog:
    wait_ready_watchdog: {}
```

A background task writes `0` as soon as it runs, then waits
`1 / watchdog_frequency` seconds between resets while the condition wait is
active. PV write time is additional to this interval, so it is not an absolute
deadline schedule. A false return from `put()` is logged.


## State and lifecycle

| Member | Behavior |
| --- | --- |
| `busy` | Local flag while the wait is active. |
| `stop()` | Sets a stop event and waits up to about one second for acknowledgement. |
| `supports_rollback()` | `False`; the scheduler skips rollback for this wait-only device. |
| `scheduler_timeout()` | Returns `None`; uses the scheduler's global `step_timeout`. |
| `get_capability(...)` | No extra capability. |

