# MonoZeroOrderDevice

`MonoZeroOrderDevice` waits until three monochromator angle readbacks satisfy a
configured zero-order criterion. It observes `alpha`, `beta`, and `theta`; it
does not command motion. Optional PVs support pause handling and watchdog
resets while waiting.

Registry type: `MonoZeroOrderDevice`  

## Configuration

| Key | Type | Required/default | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required: `MonoZeroOrderDevice` | Factory type. |
| `name` | string | Required | Device name used in presets. |
| `alpha_pv` | string | Required | Alpha angle readback PV. |
| `beta_pv` | string | Required | Beta angle readback PV. |
| `theta_pv` | string | Required | Theta angle readback PV. |
| `watchdog_pv` | string | Unset | Optional PV reset to `0` during the wait loop. |
| `pause_pv` | string | Unset | Optional PV; nonzero pauses angle sampling. |
| `options` | mapping | `{}` | Alternate location for the tuning options below. |

The following tuning keys can be configured at device level or inside
`options`. A device-level key takes precedence over the nested value.

| Tuning key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `mode` | string | `stddev` | `stddev`, `spread`, or `reference`. |
| `in_position_band` | float | `0.005` | Maximum accepted metric; must be nonnegative. |
| `deviation_threshold` | float | Alias of `in_position_band` | Canonical key wins over the alias at the same level. |
| `poll_period` | float | `0.2` seconds | Positive delay between polling rounds and pause checks. |
| `stable_reads` | integer | `3` | Number of qualifying successful samples needed. |
| `timeout` | float | `60.0` seconds | Internal deadline; zero or a negative value disables it. |
| `log_every` | integer | `10` | INFO sample-log interval, in addition to the first sample and state transitions. |
| `pv_read_timeout` | float | `0.5` seconds | Positive timeout passed to each angle PV read. |
| `read_retry_count` | integer | `3` | Read attempts per angle before a sampling round fails. |
| `max_consecutive_read_failures` | integer | `5` | Failed sampling rounds before a fatal error. |
| `reference` | mapping | Unset | Reference `alpha`, `beta`, and `theta` for reference mode. |

```yaml
devices:
  - name: zero_order
    type: MonoZeroOrderDevice
    alpha_pv: "IOC:MONO:Alpha"
    beta_pv: "IOC:MONO:Beta"
    theta_pv: "IOC:MONO:Theta"
    watchdog_pv: "IOC:MONO:Watchdog"
    pause_pv: "IOC:MONO:Pause"
    options:
      mode: spread
      in_position_band: 0.005
      poll_period: 0.2
      stable_reads: 3
      timeout: 60.0

presets:
  wait_zero_order:
    zero_order: {}
```

## Detection modes

All three PVs must use comparable engineering units. No conversion or angle
wrapping is applied. The accepted metric must be finite and no greater than
`in_position_band`.

| Mode | Metric | Interpretation |
| --- | --- | --- |
| `stddev` | Population standard deviation of the three values, using divisor `3`. | Tests agreement among alpha, beta, and theta. |
| `spread` | `max(alpha, beta, theta) - min(alpha, beta, theta)`. | Tests the largest separation among the three values. |
| `reference` | Maximum absolute error from the three corresponding reference values. | Tests each angle against its own target. |

`stddev` and `spread` compare the three angles within each sampling round, not
the time variation of one axis. Equal values can pass at any common angle; the
mode does not itself require all angles to be zero.

An out-of-band successful sample resets the stability count. Failed reads and
pause intervals do not reset that count in the supplied implementation, so
qualifying samples can be separated by a read failure or a pause.

## Pose overrides

A pose can override every tuning key above. For scalar tuning options, lookup
order is top-level pose keys, nested `pose.options`, then configured defaults.

```yaml
presets:
  wait_tighter_zero_order:
    zero_order:
      in_position_band: 0.002
      options:
        stable_reads: 5
        timeout: 120.0
```

For reference mode, all three reference values have to be defined, for example:

```yaml
presets:
  wait_reference_angles:
    zero_order:
      mode: reference
      reference:
        alpha: 0.0
        beta: 0.0
        theta: 0.0
      in_position_band: 0.005
```

## Pause, watchdog, and timeouts

A nonzero pause PV suspends angle sampling until the PV reads `0`. Watchdog
resets continue during pause polling. The adapter also writes watchdog `0`
before ordinary sampling. There is no separate watchdog frequency setting:
the reset timing depends on polling, reads, and pause handling.

## State and lifecycle

| Member | Behavior |
| --- | --- |
| `busy` | Local flag while a zero-order wait is active. |
| `stop()` | Sets the stop event and waits up to about one second for acknowledgement. |
| `supports_rollback()` | `False`; the scheduler does not capture/replay this device for rollback. |
| `scheduler_timeout()` | Returns `None`; uses the scheduler's global `step_timeout`. |
| `get_capability(...)` | No extra capability. |

