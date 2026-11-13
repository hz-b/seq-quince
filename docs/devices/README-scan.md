# ScanDevice

`ScanDevice` embeds a kiwi-scan scan as a scheduler action. It loads a scan
configuration, replaces its top-level scan dimensions with one configured
dimension, and executes the selected scan engine.

Dependencies: kiwi-scan `ScanConfig`, `ScanDimension`, and the scan factory and
configuration-loading helpers in `kiwi_scan.scan.tools`.

## Configuration

| Key | Type | Required/default | Meaning |
| --- | --- | --- | --- |
| `type` | string | Required: `ScanDevice` | Factory type. |
| `name` | string | Required | Device name used in presets. |
| `scantype` | string | Required | kiwi-scan engine name passed to `create_scan_with_config()`. |
| `config` | string | Required | Configuration identifier/path passed to the kiwi-scan loader. |
| `replacements` | mapping | Empty mapping when omitted | Values passed to the scan configuration loader. |
| `data_dir` | string | `SCAN_DATA_DIR` when set; otherwise `None` passed to the factory | Output-directory argument. |
| `actuator` | string | `energy` | Actuator name in the scan configuration. |
| `start` | float | `300.0` | Initial start position. |
| `stop` | float | `400.0` | Initial stop position. |
| `steps` | integer | `3` | Scan dimension step/point parameter interpreted by kiwi-scan. |
| `velocity` | float | `0.0` | Scan dimension velocity interpreted by the selected engine. |

```yaml
devices:
  - name: energy_scan
    type: ScanDevice
    scantype: linear
    config: mono
    replacements:
      IOC_MONO: "IOC:MONO"
    data_dir: "/path/to/scan-data"
    actuator: energy
    start: 300.0
    stop: 400.0
    steps: 11
    velocity: 0.0

presets:
  scan_energy:
    energy_scan:
      start: 350.0
      stop: 360.0
      steps: 21
  scan_with_defaults:
    energy_scan: {}
```

`mono` must resolve through the installed loader to a kiwi-scan configuration
containing the named actuator and any detectors, triggers, plugins, or output
settings needed by the scan. File suffix/path handling is delegated to that
loader. This scheduler YAML does not replace the scan's own YAML file.

## Configuration and data directories

The adapter reads the scan configuration directory from
`SCAN_CONFIG_DIR`, falling back to `get_scan_config_dir()`. It reads
`SCAN_DATA_DIR` when its own `data_dir` setting is omitted. 

## Pose and processing

| Pose key | Behavior |
| --- | --- |
| `start` | Update the first scan dimension's start position. |
| `stop` | Update its stop position. |
| `steps` | Update its step/point parameter. |
| `velocity` | Update its velocity. |

These are the only accepted dimension overrides; `actuator`, `scantype`,
`config`, and `data_dir` cannot be changed by a pose. Other keys are ignored.

## State and capabilities

| Member | Behavior |
| --- | --- |
| `snapshot()` | Returns `start`, `stop`, `steps`, and `velocity` from the first dimension, or `{}` when absent. |
| `busy` | Delegates directly to `scan.busy`. |
| `stop()` | Awaits a tracked executor call to `scan.stop()`. |
| `close()` | Releases superseded and final owned scans after operations settle; safe to repeat. |
| `get_capability("scan")` | Returns the current underlying scan object. |
| `supports_rollback()` | Returns `False`; automatic rollback is unsupported. |
| `scheduler_timeout()` | Inherited `None`; uses the scheduler's global `step_timeout`. |
Explicit `snapshot()` calls remain available. The saved scan parameters are
configuration snapshots.
