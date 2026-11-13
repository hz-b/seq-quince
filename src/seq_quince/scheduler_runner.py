import argparse
import logging
from typing import Optional

from kiwi_scan.scan.tools import set_valid_logging_level
from kiwi_scan.yaml_loader import parse_replacements

from seq_quince.runner import parse_loop_count, run_scheduler, validate_run_options

logger = logging.getLogger(__name__)


def _parse_args():
    """ Parse and validate runner options before creating devices. """
    parser = argparse.ArgumentParser(
        description="Perform different types of sequences.\n",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--replace",
        nargs="*",
        default=[],
        help="List of replacements in the form KEY=NEW_FIELD_VALUE",
    )
    parser.add_argument(
        "--config", required=True, help="Configuration for the sequence"
    )
    parser.add_argument(
        "--preset", required=True, help="Preset name configuration to be executed."
    )
    parser.add_argument(
        "--snapshot", action="store_true", help="Save a snapshot from devices"
    )
    parser.add_argument(
        "--loop",
        nargs="?",
        const="infinite",
        default="1",
        help="Run scheduler repeatedly. Use --loop N for finite runs, or --loop "
        "infinite (or bare --loop) to run until interrupted.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Delay time after each scheduler run in seconds",
    )
    parser.add_argument(
        "--log-level",
        type=int,
        choices=range(6),
        metavar="0-5",
        help="MBBO record level (0..5) to set log verbosity via scanlib helper",
    )
    args = parser.parse_args()
    try:
        loop_count = parse_loop_count(args.loop)
        validate_run_options(loop_count, args.delay, snapshot=args.snapshot)
    except ValueError as exc:
        parser.error(str(exc))

    if args.log_level is not None:
        set_valid_logging_level(args.log_level)
    return args, loop_count


def _print_iteration_start(iteration: int, loop_count: Optional[int]) -> None:
    loop_label = "∞" if loop_count is None else str(loop_count)
    print(f"▶  Run scheduler [{iteration}/{loop_label}] ", end="", flush=True)


def _print_iteration_end(_iteration: int, _loop_count: Optional[int]) -> None:
    print("✔")


def main() -> int:
    args, loop_count = _parse_args()
    try:
        replacements = parse_replacements(args.replace)
        if args.snapshot:
            print("🗂  Taking snapshot ", end="", flush=True)
        run_scheduler(
            config=args.config,
            preset=args.preset,
            replacements=replacements,
            snapshot=args.snapshot,
            loop_count=loop_count,
            delay=args.delay,
            on_iteration_start=_print_iteration_start,
            on_iteration_end=_print_iteration_end,
        )
    except Exception as exc:  # noqa: BLE001 -- CLI boundary: report execution failures.
        operation = "Snapshot" if args.snapshot else "Scheduler"
        logger.error("%s failed: %s", operation, exc)
        print("✖")
        return 1
    if args.snapshot:
        print("✔")
    return 0


def run() -> int:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s - %(filename)s - %(levelname)s - %(message)s",
    )
    try:
        return main()
    except KeyboardInterrupt:
        print("\n⏹  Scheduler loop interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(run())
