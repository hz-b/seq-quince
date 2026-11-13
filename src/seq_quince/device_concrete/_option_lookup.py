""" 
Shared by WaitConditionDevice and MonoZeroOrderDevice
Ordered option config lookup 
"""

from __future__ import annotations

from typing import Any, Mapping


def value_from_pose(
    pose: Mapping[str, Any],
    pose_options: Mapping[str, Any],
    key: str,
    default: Any,
    alias_keys: tuple[str, ...] = (),
) -> Any:
    """Prefer pose keys (including aliases) over nested option keys."""
    for candidate in (key, *alias_keys):
        if candidate in pose:
            return pose[candidate]
    for candidate in (key, *alias_keys):
        if candidate in pose_options:
            return pose_options[candidate]
    return default


def first_present(
    primary: Mapping[str, Any],
    secondary: Mapping[str, Any],
    *,
    keys: tuple[str, ...],
    default: Any,
) -> Any:
    """Return the first present key, searching all primary keys first."""
    for key in keys:
        if key in primary:
            return primary[key]
    for key in keys:
        if key in secondary:
            return secondary[key]
    return default
