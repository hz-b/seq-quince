import contextlib
import logging
import os
import stat
import tempfile
from typing import Any, Dict, Optional

import yaml

from seq_quince.loader import j2_yaml_loader
from seq_quince.tools import get_scheduler_config_dir

log = logging.getLogger("scheduler.snapshot")

# Seperated class used by SchedulerConfigManager that handles state archival
# TODO: Diff: compare snapshots
# TODO: Copy sections
# TODO: Add timestamp
# TODO: Store snapshots in a seperate folder (snapshots) with timestamped filename
# TODO: Load/Save with device description
class SnapshotManager:
    """
    Internal helper class for snapshot handling.

    This class encapsulates the logic for loading, saving, and managing
    snapshot data stored in the 'presets:' section of the configuration file.

    Not intended to be part of the public API — use SchedulerConfigManager
    for all snapshot-related interactions.
    """
    def __init__(
        self,
        config_dir: Optional[str] = None,
        preset_file: str = 'presets.yaml',
        replacements: Optional[Dict[str, str]] = None,
        log_level: int = logging.INFO,
    ):
        log.setLevel(log_level)
        self.replacements = replacements or {}
        self.config_dir = (   # TODO: config dir docu and definition
            config_dir or
            os.environ.get("SCHEDULER_CONFIG_DIR") or
            get_scheduler_config_dir()
        )
        self.preset_path = os.path.join(self.config_dir, preset_file)

    def save_snapshot(self, name: str, structure: Dict[str, Dict[str, float]]) -> None:
        """
        Save a snapshot into the 'presets:' section under the given name.

        Replace the file atomically after writing the merged data. Concurrent
        writers are not locked and may still overwrite each other's updates.

        Parameters:
            name (str): The name of the snapshot preset to store.
            structure (Dict[str, Dict[str, float]]): Device names mapped to value dictionaries.
        """
        log.info(f"Saving snapshot '{name}' to {self.preset_path}")

        file_mode = None
        if not os.path.exists(self.preset_path):
            log.warning(f"Preset file {self.preset_path} does not exist. Creating a new one.")
            data = {}
        else:
            with open(self.preset_path, 'r') as f:
                data = yaml.safe_load(f) or {}
                file_mode = stat.S_IMODE(os.fstat(f.fileno()).st_mode)

        if 'presets' not in data:
            data['presets'] = {}

        data['presets'][name] = structure

        temporary_path = None
        try:
            # The same directory keeps replacement on the destination filesystem.
            # Close explicitly below to preserve the first error if close fails too.
            f = tempfile.NamedTemporaryFile(  # noqa: SIM115
                mode='w', encoding='utf-8',
                dir=os.path.dirname(os.path.abspath(self.preset_path)),
                prefix='.' + os.path.basename(self.preset_path) + '.',
                suffix='.tmp', delete=False,
            )
            temporary_path = f.name
            try:
                yaml.safe_dump(data, f, sort_keys=False)
                f.flush()
            except BaseException:
                # A close-time flush error must not replace the original failure.
                with contextlib.suppress(OSError):
                    f.close()
                raise
            else:
                f.close()
            if file_mode is not None:
                os.chmod(temporary_path, file_mode)
            os.replace(temporary_path, self.preset_path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                # Cleanup must not obscure a serialization, write or replace error.
                with contextlib.suppress(OSError):
                    os.unlink(temporary_path)

        log.debug(f"Snapshot '{name}' successfully saved.")

    def load_snapshot(self) -> Dict[str, Any]:
        if not os.path.exists(self.preset_path):
            return {}
        cfg = j2_yaml_loader(self.preset_path, self.replacements)
        log.debug(f"Load snapshot: {cfg}")
        return cfg




