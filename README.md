# seq-quince

`seq-quince`: An asynchronous, YAML-configured device scheduler for EPICS beamlines, built on [kiwi-scan](https://github.com/hz-b/kiwi-scan).

Devices (motors, actuators, hexapods, PV sequences, scans, wait conditions, …) are declared in a
`beamline.yaml`. Named **presets** map devices to target poses. The scheduler turns a preset into a sequence of move
steps, runs them serially or in parallel groups, and rolls completed steps back to their pre-run snapshot if a step fails.


## Overview of Features

-   **YAML + Jinja2 configuration** – `beamline.yaml` and `presets.yaml` are rendered as Jinja2 templates; both `${TOKEN}` and
    `{{ TOKEN }}` placeholders are supported, as well as `{% include %}` from sibling/parent directories.
-   **Device abstraction** – every device implements `DeviceABC` (`process`, `snapshot`, `busy`, `stop`, optional capabilities).
-   **Device factory** – devices are created from config by their `type:` key; custom types can be registered at runtime.
-   **Custom device extensions** — Load external device modules and packages via SEQ_QUINCE_DEVICE_PATH, with explicit registration through DEVICE_CLASSES.
-   **Sequences** – presets are converted into `MoveStep` lists with `serial` or `parallel` grouping and barriers.
-   **Async scheduler** – concurrent execution per group, optional per-step timeouts, best-effort rollback on failure.
-   **Snapshots** – current device read-backs can be saved as a new preset (`presets.yaml`).
-   **kiwi-scan integration** – `ScanDevice` runs any kiwi-scan scan type, `ActuatorDevice`/`HexapodDevice` use kiwi-scan actuators.

## Installation

```bash
pip install seq-quince
```

For development, clone the repository and install it in editable mode:

```bash
pip install -e .
```


## Command line tools

| Command | Purpose |
|---------|---------|
| `scheduler_runner` | Run a preset (`--config`, `--preset`, `--loop`, `--delay`, `--snapshot`, `--replace`) |
| `device_runner` | Run one device from a preset; see [device runner](docs/device_runner.md). |

Example:

```bash
scheduler_runner --config u171dcm1 --preset branch --replace BL=U171
```

## Configuration

See [docs/devices.md](docs/devices.md) and [docs/devices](docs/devices) for configuration details.

```yaml
# beamline.yaml
devices:
  - name: simulation
    type: SimDevice
    delay: 0.1
  - name: mono_energy
    type: ActuatorDevice
    config: { ... }          # kiwi-scan ActuatorConfig

presets:
  branch:
    simulation:  { position: 10.0 }
    mono_energy: { position: 400.0 }
```

Presets from `presets.yaml` (written by `--snapshot`) override presets of the same name in `beamline.yaml`.
Unknown preset names raise `ValueError`; an explicitly configured empty preset
(`empty: {}`) remains valid. Device names must be unique: duplicates are rejected
before any devices are constructed.

After template rendering, YAML is parsed with `yaml.safe_load()`. Python-specific
YAML tags are rejected. Normal loading does not print the rendered configuration.

Environment variables used by the code:

| Variable               | Used for                                                        |
|------------------------|-----------------------------------------------------------------|
| `SCHEDULER_CONFIG_DIR` | Direct configuration directory; overrides beamline-name lookup. |
| `SEQ_QUINCE_CONFIG_DIR` | Existing base directory; looks in `<dir>/scheduler_config/beamlines/<config>` when no direct override is set. |
| `SCAN_CONFIG_DIR`      | kiwi-scan config directory for `ScanDevice`                     |
| `SCAN_DATA_DIR`        | Output directory for `ScanDevice` scans                         |

See [docs/devices.md](docs/devices.md) for the built-in device types and their configuration keys.

## Development

```bash
make help      # list targets
make lint      # pylint, ruff, pyright
make test      # pytest
make build     # sdist + wheel
make install_completion # install bash completion scripts
make uninstall_completion # uninstall bash completion scripts
make clean     # Remove local virtualenv and python generated files
make cscope    # Build cscope and ctags indexes
make tag       # create git tag
```
## License

MIT – see [LICENSE](LICENSE).
