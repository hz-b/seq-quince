import argparse
import logging

from kiwi_scan.scan.tools import set_valid_logging_level
from kiwi_scan.yaml_loader import parse_replacements

from seq_quince.runner import run_device

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one device's entry from a named preset."
    )
    parser.add_argument("--config", required=True, help="Beamline configuration name")
    parser.add_argument("--device", required=True, help="Configured device name")
    parser.add_argument(
        "--preset", required=True, help="Preset containing the device entry"
    )
    parser.add_argument(
        "--replace",
        nargs="*",
        default=[],
        help="Configuration replacements in the form KEY=VALUE",
    )
    parser.add_argument(
        "--log-level",
        type=int,
        choices=range(6),
        metavar="0-5",
        help="MBBO record level (0..5) to set log verbosity",
    )
    args = parser.parse_args()
    if args.log_level is not None:
        set_valid_logging_level(args.log_level)
    return args


def main() -> int:
    args = _parse_args()
    try:
        replacements = parse_replacements(args.replace)
        run_device(
            config=args.config,
            device_name=args.device,
            preset=args.preset,
            replacements=replacements,
        )
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # Report any configuration/device failure.
        logger.error("Device '%s' preset '%s' failed: %s", args.device, args.preset, exc)
        return 1
    print(f"✔ Device '{args.device}' completed preset '{args.preset}'")
    return 0


def run() -> int:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s - %(filename)s - %(levelname)s - %(message)s",
    )
    try:
        return main()
    except KeyboardInterrupt:
        print("\n⏹ Device execution interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(run())
