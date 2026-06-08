"""Windows MTP device access wrapper."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import mtp.win_access as win_access

from transfer.settings import DEFAULT_FOLDERS

WPD_CONTENT_TYPE_FILE = 2
WPD_CONTENT_TYPE_DIRECTORY = 1

_cached_devices: list[Any] | None = None
_cache_thread_id: int | None = None


@dataclass
class DeviceInfo:
    index: int
    name: str
    description: str
    serial: str
    devicename: str


@dataclass
class PhoneFile:
    content_path: str
    display_path: str
    filename: str
    size: int
    date_modified: datetime
    folder_path: str


def reset_device_cache(*, close_sessions: bool = False) -> None:
    """Drop cached MTP handles (call before re-scanning devices)."""
    global _cached_devices, _cache_thread_id
    if close_sessions and _cached_devices:
        for device in _cached_devices:
            wpd_device = getattr(device, "_device", None)
            if wpd_device is None:
                continue
            try:
                wpd_device.Close()
            except Exception:
                pass
    _cached_devices = None
    _cache_thread_id = None


def refresh_open_device(index: int) -> Any:
    """Reconnect to the phone and return a fresh device handle."""
    reset_device_cache(close_sessions=True)
    return open_device(index)


def get_devices() -> list[Any]:
    """Return one stable list of open device handles for this process/thread."""
    global _cached_devices, _cache_thread_id
    thread_id = threading.get_ident()
    if _cached_devices is not None and _cache_thread_id != thread_id:
        # Streamlit reruns may execute on a different thread; COM handles are not portable.
        _cached_devices = None
        _cache_thread_id = None
        win_access.reset_com_state()
    if _cached_devices is None:
        _cached_devices = win_access.get_portable_devices()
        _cache_thread_id = thread_id
    return _cached_devices


def list_devices() -> list[DeviceInfo]:
    devices = get_devices()
    return [
        DeviceInfo(
            index=index,
            name=device.name,
            description=device.description,
            serial=device.serialnumber,
            devicename=device.devicename,
        )
        for index, device in enumerate(devices)
    ]


def open_device(index: int) -> Any:
    devices = get_devices()
    if index < 0 or index >= len(devices):
        raise ValueError("Selected device is not available")
    return devices[index]


def close_device(device: Any) -> None:
    # Keep the shared MTP session alive for the Streamlit process.
    del device


def download_to_path(device: Any, content_path: str, output_path: Path) -> None:
    content = win_access.get_content_from_device_path(device, content_path)
    if content is None:
        raise FileNotFoundError(f"MTP file not found: {content_path}")
    content.download_file(str(output_path))


def _pick_storage(device: Any) -> Any:
    storages = device.get_content()
    if not storages:
        raise IOError("No storage found on device")
    for storage in storages:
        lowered = storage.name.lower()
        if "internal" in lowered or "shared" in lowered:
            return storage
    return storages[0]


def mtp_path(device: Any, *parts: str) -> str:
    """Build a full MTP path: devicename/storage/part1/part2/..."""
    storage = _pick_storage(device)
    return "/".join([device.devicename, storage.name, *parts])


def _relative_display_path(full_path: str, storage_name: str) -> str:
    normalized = full_path.replace("\\", "/")
    marker = f"/{storage_name}/"
    if marker in normalized:
        return normalized.split(marker, 1)[1]
    parts = normalized.split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:]) if len(parts) == 2 else "/".join(parts[-3:])
    return normalized


def get_storage(device: Any) -> Any:
    return _pick_storage(device)


def list_dcim_folders(device: Any) -> list[str]:
    storage = _pick_storage(device)
    dcim = storage.get_path("DCIM")
    if dcim is None:
        return []
    return sorted(
        child.name
        for child in dcim.get_children()
        if child.content_type == WPD_CONTENT_TYPE_DIRECTORY
    )


def default_folder_selection(available: list[str]) -> list[str]:
    selected = [name for name in DEFAULT_FOLDERS if name in available]
    return selected or [name for name in available if name in {"Camera", "OpenCamera"}]


def resolve_folder_path(device: Any, folder_name: str) -> str | None:
    path = mtp_path(device, "DCIM", folder_name)
    if win_access.get_content_from_device_path(device, path) is None:
        return None
    return path


def _walk_mtp_files(device: Any, dir_path: str, storage_name: str, folder_path: str) -> Iterator[PhoneFile]:
    dir_content = win_access.get_content_from_device_path(device, dir_path)
    if dir_content is None:
        return

    for child in dir_content.get_children():
        child_path = f"{dir_path}/{child.name}"
        if child.content_type == WPD_CONTENT_TYPE_DIRECTORY:
            yield from _walk_mtp_files(device, child_path, storage_name, folder_path)
        elif child.content_type == WPD_CONTENT_TYPE_FILE:
            display_path = _relative_display_path(child_path, storage_name)
            yield PhoneFile(
                content_path=child_path,
                display_path=display_path,
                filename=child.name,
                size=child.size,
                date_modified=child.date_modified,
                folder_path=folder_path,
            )


def iter_folder_files(device: Any, folder_name: str) -> Iterator[PhoneFile]:
    storage = _pick_storage(device)
    root_path = mtp_path(device, "DCIM", folder_name)
    if win_access.get_content_from_device_path(device, root_path) is None:
        return

    yield from _walk_mtp_files(device, root_path, storage.name, root_path)


def count_folder_files(device: Any, folder_name: str) -> int:
    return sum(1 for _ in iter_folder_files(device, folder_name))


def delete_folder(device: Any, folder_name: str) -> None:
    folder_path = resolve_folder_path(device, folder_name)
    if folder_path is None:
        raise FileNotFoundError(f"Folder not found on device: DCIM/{folder_name}")

    folder = win_access.get_content_from_device_path(device, folder_path)
    if folder is None:
        raise FileNotFoundError(f"Folder not found on device: DCIM/{folder_name}")

    folder.remove()
