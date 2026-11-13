from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Type, TypeVar


def get_param(config: Dict[str, Any], key: str, typ, default=None, required=False):
    value = config.get(key, default)
    if required and value is None:
        raise ValueError(f"Missing required parameter: '{key}'")
    if value is not None and not isinstance(value, typ):
        try:
            value = typ(value)
        except (TypeError, ValueError) as e:
            raise TypeError(
                f"Parameter '{key}' expects type {typ.__name__} "
                f"but got {value!r} ({e})"
            ) from e
    return value



T = TypeVar("T", bound="DeviceABC")

class DeviceABC(ABC):
    """Abstract base class for devices."""

    # init free configurable
    # ------ Data & state ----------------------------------------------------
    name: str                                  # human-readable identifier


    @classmethod
    @abstractmethod
    def from_config_dict(cls: Type[T], config: Dict[str, Any]) -> T:
        """
        Create an instance of the device from a config dictionary.
        """

    # ------ Motion primitives ----------------------------------------------
    # async def move_to_pose(self, pose: Dict[str, float]) -> None:
    @abstractmethod
    async def process(self, pose: Dict[str, float]) -> None:
        """
        Drive the device so every axis reaches the *pose* target (value in
        engineering units).
        Must raise error *only* after an orderly rollback has completed or is impossible.
        """

    @abstractmethod
    async def snapshot(self) -> Dict[str, float]:
        """Return the *current* read-back values for every axis."""

    @property
    @abstractmethod
    def busy(self) -> bool:
        """True while **any** axis is still moving or the device is faulted."""

    @abstractmethod
    async def stop(self) -> None:
        """
        Best-effort immediate stop (e.g. user pressed Ctrl-C).  Must leave the
        device in a recoverable, well-defined state.
        """

    async def close(self) -> None:
        """
        Release owned resources after operations settle; safe to repeat.
        """
        from seq_quince.device.lifecycle import close_resources

        await close_resources(self)

    def get_capability(self, name: str) -> Optional[Any]:
        """Return the capability object by name, or None if not present."""
        return None  # Default: no capability
    def supports_rollback(self) -> bool:
        """
        Whether the scheduler should attempt rollback for this device.

        Motion devices usually return True.
        Wait-only / monitor-only devices should return False.
        """
        return True

    def scheduler_timeout(self, pose: Optional[Dict[str, Any]] = None) -> Optional[float]:
        """
        Optional per-step timeout override used by Scheduler.

        Return:
          * None  -> inherit the scheduler's global step_timeout
          * > 0   -> scheduler-enforced timeout in seconds
          * <= 0  -> treated as disabled (no scheduler timeout)

        The default is None, so the global timeout applies when configured.
        """
        return None

