import logging
import os
from typing import Any, Dict, List, Optional

import yaml

from seq_quince.loader import j2_yaml_loader
from seq_quince.sequence import MoveStep, build_sequence
from seq_quince.snapshot import SnapshotManager
from seq_quince.tools import get_scheduler_config_dir, merge_replacements

log = logging.getLogger("seq_quince.loader")

#TODO: Expand macros
#TODO: Templates with Jinja2
#TODO: Defaults
class SchedulerConfigManager:
    def __init__(
        self,
        config_dir: Optional[str] = None,
        replacements: Optional[Dict[str, str]] = None,
        config_file: str = 'beamline.yaml',
        preset_file: str = 'presets.yaml',
        log_level: int = logging.INFO,
    ):
        log.setLevel(log_level)
        self.replacements = replacements or {}
        self.config_dir = (
            config_dir or
            os.environ.get("SCHEDULER_CONFIG_DIR") or
            get_scheduler_config_dir()
        )
        log.info(f"Scheduler config directory: {self.config_dir}")
        self.config_path = os.path.join(self.config_dir, config_file)
        self.snapshot_manager = SnapshotManager(config_dir=self.config_dir, preset_file=preset_file, replacements=self.replacements)
        self.config = self._load_config()
        log.info(f"Scheduler config loaded: {self.config_path}")
        self.devices_cfg = self.config.get("devices", [])
        self.macros = self.config.get("macros", {})
        self.presets = self.config.get("presets", {})
        self.sequence = self.config.get("sequence", {})

    def _load_config(self) -> Dict[str, Any]:
        log.info(f"yaml_load: {self.config_path}, {self.replacements}")
        cfg = j2_yaml_loader(self.config_path, self.replacements)
        log.debug(f"cfg: {cfg}")
        user_presets = self.snapshot_manager.load_snapshot()

        # Merge presets: user_presets overrides cfg
        cfg_presets = cfg.get("presets", {})
        user_presets_only = user_presets.get("presets", {})
        merged_presets = {**cfg_presets, **user_presets_only}

        # Insert merged presets back into cfg
        cfg["presets"] = merged_presets
        log.debug(f"cfg merged: {cfg}")
        return cfg

    def get_device_configs(self) -> List[Dict[str, Any]]:
        return self.devices_cfg

    def create_devices(self, factory) -> Dict[str, Any]:
        names = set()
        for cfg in self.devices_cfg:
            name = cfg.get("name")
            if name in names:
                raise ValueError(f"Duplicate device name '{name}' in config.")
            names.add(name)
        for cfg in self.devices_cfg:
            merge_replacements(cfg, self.replacements)
        devices = [factory.create(cfg) for cfg in self.devices_cfg]
        return {d.name: d for d in devices}

    def get_device_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        return next((cfg for cfg in self.devices_cfg if cfg.get("name") == name), None)

    def get_preset(self, preset_name: str) -> Dict[str, Any]:
        if preset_name not in self.presets:
            raise ValueError(f"Preset '{preset_name}' not found in config.")
        return self.presets[preset_name]

    def get_sequence_config(self) -> Dict[str, Any]:
        return self.sequence

    def get_macros(self) -> Dict[str, str]:
        return self.macros

    def build_sequence_from_preset(
        self,
        preset_name: str,
        devices: dict,
        *,
        default_grouping: str = "parallel",
        build_sequence_func=None,  # allow for test/mock override
    ) -> List["MoveStep"]:
        """
        Build a MoveStep sequence for a given preset using the provided devices dict.
        """
        # Get the preset mapping
        preset = self.get_preset(preset_name)
        if preset is None:
            raise ValueError(f"Preset '{preset_name}' not found in config.")
        # Use external or default build_sequence function
        if build_sequence_func is None:
            build_sequence_func = build_sequence
        # Use the preset as the 'target'
        return build_sequence_func(devices, preset, default_grouping=default_grouping)

    # TODO: Implement a method to update full config (takes effact before device creation)
    def update_preset_item(self, preset_name: str, device_name: str, updates: dict) -> None:
        """
        Update one device's preset entry with new values.
        Example: update_preset_item('init', 'energy_scan', {'start': 355.0})
        """
        preset = self.presets.setdefault(preset_name, {})
        device_preset = preset.setdefault(device_name, {})
        if not isinstance(device_preset, dict):
            raise TypeError(f"Preset entry for {device_name} in '{preset_name}' must be a dict.")
        device_preset.update(updates)

    def save_snapshot(self, name: str, structure: Dict[str, Dict[str, float]]) -> None:
        self.snapshot_manager.save_snapshot(name, structure)

    def save_config(self, path: Optional[str] = None) -> None:
        path = path or self.config_path
        # TODO: If you have self.replacements, replace macros before writing if needed
        with open(path, "w") as f:
            yaml.dump(self.config, f, default_flow_style=False)

