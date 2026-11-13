import argparse
import asyncio
import logging
import os

from kiwi_scan.yaml_loader import parse_replacements, yaml_loader

from seq_quince.device.lifecycle import DeviceScope, own_device
from seq_quince.device_concrete.actuator import ActuatorDevice
from seq_quince.device_concrete.ophyd_motor import OphydMotorDevice
from seq_quince.device_concrete.scan import ScanDevice
from seq_quince.device_concrete.sequence import SequenceDevice
from seq_quince.device_concrete.sim import SimDevice
from seq_quince.device_concrete.toggle import ToggleDevice
from seq_quince.tools import get_scheduler_config_dir, merge_replacements

logger = logging.getLogger(__name__)

async def _check_device(device_class, config, pose):
    try:
        async with DeviceScope() as scope:
            device = await scope.run_blocking(device_class.from_config_dict, config)
            own_device(device)
            await device.process(pose)
            print(await device.snapshot())
            print("Busy?", device.busy)
            await device.stop()
    except Exception:
        logger.exception("Device check failed")

async def main():
    parser = argparse.ArgumentParser(
        description="Perform different types of sequences.\n",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument('--replace', nargs='*', help='List of replacements in the form KEY=NEW_FIELD_VALUE', default=[])
    parser.add_argument("--config", required=True, help="Preset configuration for the sequence", default="u171dcm1")
    args = parser.parse_args()
    replacements = parse_replacements(args.replace)
    config_dir = os.environ.get("SCHEDULER_CONFIG_DIR")
    if config_dir is None:
        config_dir = get_scheduler_config_dir(args.config)

    async with DeviceScope() as scope:
        scheduler_config = await scope.run_blocking(
            yaml_loader, os.path.join(config_dir, "beamline.yaml"), replacements
        )
    logger.info(f"Scheduler config directory: {config_dir}")
    logger.info(f"Scheduler config: {scheduler_config}")
    devices = scheduler_config["devices"]

    checks = (
        ("simulation", "sim_device", SimDevice, {"position": 100.0}),
        ("mono_energy", "actuator_device", ActuatorDevice, {"position": 100.0}),
        ("motor_record", "motor_record_device", OphydMotorDevice, {"position": 10.0}),
        ("configure_scan", "sequence_device", SequenceDevice, {}),
        ("diode", "diode_device", ToggleDevice, {}),
        ("energy_scan", "energy_scan_device", ScanDevice, {}),
    )
    for name, label, device_class, pose in checks:
        config = next(dev for dev in devices if dev["name"] == name)
        merge_replacements(config, replacements)
        logger.info(f"{label}: {config}")
        await _check_device(device_class, config, pose)

def run():
    asyncio.run(main())

if __name__ == "__main__":
    run()

