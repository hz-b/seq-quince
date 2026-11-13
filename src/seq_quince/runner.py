"""Reusable sequence and snapshot operations, independent of the CLI.

Async callers can use run_sequence, run_sequences or take_snapshot with a
configuration manager. Synchronous callers can use run_scheduler to load a
configuration and own an event loop for the whole operation. Exceptions,
cancellation and KeyboardInterrupt propagate to the caller.

For one device, await run_device_preset or call run_device synchronously.
Only the selected device is constructed; the scheduler runs one MoveStep.
"""

import asyncio
import itertools
import logging
import math
import os
from collections.abc import Mapping
from copy import deepcopy
from typing import Callable, Dict, Optional

from seq_quince.config_manager import SchedulerConfigManager
from seq_quince.device.device_abs import DeviceABC
from seq_quince.device.factory import DeviceFactory
from seq_quince.device.lifecycle import DeviceScope, own_device
from seq_quince.scheduler import Scheduler
from seq_quince.sequence import MoveStep
from seq_quince.tools import get_scheduler_config_dir, merge_replacements

logger = logging.getLogger(__name__)

Snapshot = Dict[str, Dict[str, float]]
IterationCallback = Callable[[int, Optional[int]], None]


def parse_loop_count(raw_value: object) -> Optional[int]:
    """Normalize a positive count or an infinite/inf/forever string."""
    value = str(raw_value).strip().lower()
    if value in {"infinite", "inf", "forever"}:
        return None

    try:
        count = int(value)
    except ValueError as exc:
        raise ValueError("--loop expects a positive integer or 'infinite'") from exc

    if count < 1:
        raise ValueError("--loop expects a positive integer or 'infinite'")
    return count

def validate_run_options(loop_count: Optional[int], delay: float, *, snapshot: bool = False) -> None:
    """ Reject invalid execution options before loading or creating devices. """
    if loop_count is not None and (
        isinstance(loop_count, bool)
        or not isinstance(loop_count, int)
        or loop_count < 1
    ):
        raise ValueError("loop_count must be a positive integer or None")
    if (
        isinstance(delay, bool)
        or not isinstance(delay, (int, float))
        or not math.isfinite(delay)
        or delay < 0
    ):
        raise ValueError("delay must be a finite non-negative number")
    if snapshot and loop_count != 1:
        raise ValueError(
            "--loop can only be used when running the scheduler, not with --snapshot"
        )

def create_config_manager(config: str, replacements: Optional[Dict[str, str]] = None) -> SchedulerConfigManager:
    """ Load beamline.yaml, check SCHEDULER_CONFIG_DIR before the config name. """
    config_dir = os.environ.get("SCHEDULER_CONFIG_DIR")
    if config_dir is None:
        config_dir = get_scheduler_config_dir(config)
    return SchedulerConfigManager(
        config_dir=config_dir,
        replacements=replacements,
        config_file="beamline.yaml",
    )

def create_devices(config_manager: SchedulerConfigManager) -> Dict[str, DeviceABC]:
    """ Create one fresh set of configured devices using the standard factory. """
    devices = config_manager.create_devices(DeviceFactory)
    for device in devices.values():
        own_device(device)
    logger.info("Creating devices done")
    return devices

def create_scheduler(config_manager: SchedulerConfigManager, preset: str) -> Scheduler:
    """Create fresh devices and build a serial preset with no step timeout."""
    devices = create_devices(config_manager)
    steps = config_manager.build_sequence_from_preset(
        preset, devices, default_grouping="serial"
    )
    logger.info("steps: %s", steps)
    return Scheduler(steps, step_timeout=None)

def create_device_step(config_manager: SchedulerConfigManager, device_name: str, preset: str) -> MoveStep:
    """ 
    Validate a selection, then construct only its device and copy its pose.
    """
    if not isinstance(device_name, str) or not device_name.strip():
        raise ValueError("device_name must be a nonempty string")
    if not isinstance(preset, str) or not preset.strip():
        raise ValueError("preset must be a nonempty string")
    presets = config_manager.presets
    if not isinstance(presets, Mapping):
        raise TypeError("presets must be a mapping")
    if preset not in presets:
        raise ValueError(f"Preset '{preset}' not found in config")
    target = presets[preset]
    if not isinstance(target, Mapping):
        raise TypeError(f"Preset '{preset}' must be a mapping")
    if device_name not in target:
        raise ValueError(f"Device '{device_name}' has no entry in preset '{preset}'")
    pose = target[device_name]
    if not isinstance(pose, Mapping):
        raise TypeError(f"Entry for device '{device_name}' in preset '{preset}' must be a mapping")
    matches = [
        cfg
        for cfg in config_manager.get_device_configs()
        if isinstance(cfg, Mapping) and cfg.get("name") == device_name
    ]
    if not matches:
        raise ValueError(f"Device '{device_name}' not found in config")
    if len(matches) > 1:
        raise ValueError(f"Device '{device_name}' is configured more than once")
    device_config = deepcopy(dict(matches[0]))
    selected_pose = deepcopy(dict(pose))
    merge_replacements(device_config, config_manager.replacements)
    device = DeviceFactory.create(device_config)
    own_device(device)
    return MoveStep(device, selected_pose, group_id=0, description=device_name)

async def run_device_preset(config_manager: SchedulerConfigManager, device_name: str, preset: str) -> None:
    """
    Run one device's preset entry with the existing scheduler timeouts.
    Construct hardware in a tracked worker. 
    """
    async with DeviceScope() as scope:
        step = await scope.run_blocking(create_device_step, config_manager, device_name, preset)
        scheduler = Scheduler([step], step_timeout=None)
        await scheduler.run()

def run_device(
    config: str,
    device_name: str,
    preset: str,
    *,
    replacements: Optional[Dict[str, str]] = None,
) -> None:
    """
    Load the standard config and execute one device's named preset once.
    Runs run_device_preset() in own event loop. 
    """
    config_manager = create_config_manager(config, replacements)
    asyncio.run(run_device_preset(config_manager, device_name, preset))

async def take_snapshot(
    config_manager: SchedulerConfigManager, preset: str
) -> Snapshot:
    """ Read the preset's devices, save the snapshot and return it without moving. """
    async with DeviceScope() as scope:
        scheduler = await scope.run_blocking(create_scheduler, config_manager, preset)
        snapshot = await scheduler.snapshot()
        await scope.run_blocking(config_manager.save_snapshot, preset, snapshot)
        return snapshot


async def run_sequence(config_manager: SchedulerConfigManager, preset: str) -> None:
    """ Build and execute one preset using fresh devices. """
    async with DeviceScope() as scope:
        scheduler = await scope.run_blocking(create_scheduler, config_manager, preset)
        await scheduler.run()


async def run_sequences(
    config_manager: SchedulerConfigManager,
    preset: str,
    *,
    loop_count: Optional[int] = 1,
    delay: float = 0.0,
    on_iteration_start: Optional[IterationCallback] = None,
    on_iteration_end: Optional[IterationCallback] = None,
) -> None:
    """
    Repeat a preset; None means run until interrupted or cancelled.
    """
    validate_run_options(loop_count, delay)
    iterations = itertools.count(1) if loop_count is None else range(1, loop_count + 1)
    for iteration in iterations:
        if on_iteration_start is not None:
            on_iteration_start(iteration, loop_count)
        await run_sequence(config_manager, preset)
        if on_iteration_end is not None:
            on_iteration_end(iteration, loop_count)
        await asyncio.sleep(delay)


def run_scheduler(
    config: str,
    preset: str,
    *,
    replacements: Optional[Dict[str, str]] = None,
    snapshot: bool = False,
    loop_count: Optional[int] = 1,
    delay: float = 0.0,
    on_iteration_start: Optional[IterationCallback] = None,
    on_iteration_end: Optional[IterationCallback] = None,
) -> Optional[Snapshot]:
    """
    Load a configuration and run sequences or save one snapshot synchronously.
    Returns the saved mapping for a snapshot if snapshot is defined, 
    otherwise None for running a sequence. 
    """
    validate_run_options(loop_count, delay, snapshot=snapshot)
    config_manager = create_config_manager(config, replacements)
    if snapshot:
        return asyncio.run(take_snapshot(config_manager, preset))
    asyncio.run(
        run_sequences(
            config_manager,
            preset,
            loop_count=loop_count,
            delay=delay,
            on_iteration_start=on_iteration_start,
            on_iteration_end=on_iteration_end,
        )
    )
    return None
