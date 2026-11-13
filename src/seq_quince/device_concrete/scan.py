import inspect
import os
from copy import deepcopy
from typing import Any, Dict, Optional

from kiwi_scan.datamodels import ScanConfig, ScanDimension
from kiwi_scan.scan.tools import (
    create_scan_with_config,
    get_scan_config_dir,
    load_scan_config_from_file,
)

from seq_quince.device.device_abs import DeviceABC, get_param
from seq_quince.device.lifecycle import (
    ensure_open,
    has_pending_work,
    release_resource,
    run_blocking,
    run_operation,
    track_resource,
)


class ScanDevice(DeviceABC):
    """
    A device that wraps a Scan instance (of any scan type), exposing it as a capability.
    """
    _cleanup_tracks_work = True

    def __init__(
        self,
        name: str,
        scantype: str,
        config: str,
        replacements: Optional[Dict[str, str]] = None,
        data_dir: Optional[str] = None,
        *,
        actuator: str = "energy",
        start: float = 300.0,
        stop: float = 400.0,
        steps: int = 3,
        velocity: float = 0.0
    ):
        self.name = name
        scan_config_dir = os.environ.get("SCAN_CONFIG_DIR", get_scan_config_dir())
        if replacements is None:
            replacements = {}
        scan_config_raw = load_scan_config_from_file(scan_config_dir, config, replacements)
        scan_config = ScanConfig.from_dict(scan_config_raw)
        dim = ScanDimension(actuator, start, stop, steps, velocity)
        scan_config.scan_dimensions = [ dim ]
        if data_dir is None:
            data_dir = os.environ.get("SCAN_DATA_DIR")
        self._scan = self._create_owned_scan(scantype, scan_config, data_dir)
        self._scantype = scantype
        self._config = scan_config
        self._data_dir = data_dir
        self._stop_requested = False

    @classmethod
    def from_config_dict(cls, config: Dict[str, Any]) -> "ScanDevice":
        """ TODO: helper from kiwi """
        name = get_param(config, "name", str, required=True)
        scantype = get_param(config, "scantype", str, required=True)
        config_file = get_param(config, "config", str, required=True)
        replacements = get_param(config, "replacements", dict, default=None)
        data_dir = get_param(config, "data_dir", str, default=None)
        actuator = get_param(config, "actuator", str, default="energy")
        start = get_param(config, "start", float, default=300.0)
        stop = get_param(config, "stop", float, default=400.0)
        steps = get_param(config, "steps", int, default=3)
        velocity = get_param(config, "velocity", float, default=0.0)
        if name is None:
            raise ValueError("Missing required parameter: 'name'")
        if scantype is None:
            raise ValueError("Missing required parameter: 'scantype'")
        if config_file is None:
            raise ValueError("Missing required parameter: 'config'")
        if actuator is None:
            raise ValueError("Parameter 'actuator' must not be None")
        if start is None:
            raise ValueError("Parameter 'start' must not be None")
        if stop is None:
            raise ValueError("Parameter 'stop' must not be None")
        if steps is None:
            raise ValueError("Parameter 'steps' must not be None")
        if velocity is None:
            raise ValueError("Parameter 'velocity' must not be None")
        return cls(
            name=name,
            scantype=scantype,
            config=config_file,
            replacements=replacements,
            data_dir=data_dir,
            actuator=actuator,
            start=start,
            stop=stop,
            steps=steps,
            velocity=velocity,
        )

    def get_capability(self, name: str) -> Optional[Any]:
        if name == "scan":
            if self._scan is None:
                return super().get_capability(name)
            return self._scan
        return super().get_capability(name)

    async def process(self, pose: Dict[str, float]) -> None:
        """
        Optionally update config with pose values, then start scan.
        """
        ensure_open(self)
        self._stop_requested = False
        # For example, update the first scan dimension in config (if present)
        scan = self.get_capability("scan")
        if scan is None:
            raise RuntimeError("Scan object could not be created")

        # Build against a copy: failed construction must leave the current scan
        # and its advertised parameters intact.
        dim_keys = {'start', 'stop', 'steps', 'velocity'}
        updates = {key: value for key, value in pose.items() if key in dim_keys}
        if updates and self._config.scan_dimensions:
            config = deepcopy(self._config)
            dimensions = config.scan_dimensions
            if not dimensions:
                raise RuntimeError("Scan dimensions unavailable for replacement")
            dimension = dimensions[0]
            for key, value in updates.items():
                setattr(dimension, key, value)
            replacement = await run_blocking(
                self, self._create_owned_scan, self._scantype, config, self._data_dir
            )
            if self._stop_requested:
                # The worker owns its result even when stop arrived mid-build.
                return
            previous = scan
            self._scan = replacement
            self._config = config
            scan = replacement
            # Keep both registered until disposal succeeds. In particular, a
            # failed close cannot lose the superseded scan from final cleanup.
            await release_resource(self, previous)

        if self._stop_requested:
            return
        if inspect.iscoroutinefunction(scan.execute):
            await run_operation(self, scan.execute())
        else:
            await run_blocking(self, scan.execute)

    def _create_owned_scan(self, scantype, config, data_dir):
        scan = create_scan_with_config(scantype, config, data_dir)
        if scan is None:
            raise RuntimeError("Scan object could not be created")
        # Register in the worker as well as during initial construction: the
        # caller may be cancelled before a replacement worker returns its scan.
        track_resource(self, scan, scan.close)
        return scan

    async def snapshot(self) -> Dict[str, float]:
        """
        Return the current scan parameters (from the first scan dimension).
        """
        ensure_open(self)
        if self._config.scan_dimensions and len(self._config.scan_dimensions) > 0:
            dim = self._config.scan_dimensions[0]
            return {
                "start": dim.start,
                "stop": dim.stop,
                "steps": dim.steps,
                "velocity": getattr(dim, "velocity", 0.0),
            }
        else:
            return {}

    def supports_rollback(self) -> bool:
        """ Scan parameters describe configuration; replay would acquire again. """
        return False
    
    @property
    def busy(self) -> bool:
        scan = self.get_capability("scan")
        if scan is None:
            raise RuntimeError("Scan object could not be created")
        return scan.busy or has_pending_work(self)

    async def stop(self) -> None:
        self._stop_requested = True
        scan = self.get_capability("scan")
        if scan is None:
            raise RuntimeError("Scan object could not be created")
        await run_blocking(self, scan.stop)

