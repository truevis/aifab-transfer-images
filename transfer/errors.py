"""Shared transfer error helpers."""

from __future__ import annotations

import errno


class DiskFullError(OSError):
    """Raised when the destination drive has no free space."""


def is_disk_full_error(exc: BaseException) -> bool:
    if isinstance(exc, DiskFullError):
        return True
    if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, 112}:  # 112 = ERROR_DISK_FULL (Windows)
        return True
    message = str(exc).lower()
    return any(
        phrase in message
        for phrase in (
            "no space left on device",
            "not enough space",
            "disk full",
            "there is not enough space",
        )
    )


def is_resource_in_use_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(
        phrase in message
        for phrase in (
            "resource is in use",
            "requested resource is in use",
            "device is busy",
            "sharing violation",
        )
    )
