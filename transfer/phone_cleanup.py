"""Delete verified folders from the phone."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from transfer.events import TransferEvent
from transfer.stop import ShouldStop, is_cancelled
from transfer.filters import filter_reason
from transfer.mtp_client import PhoneFile, delete_folder, iter_folder_files, resolve_folder_path
import mtp.win_access as win_access


@dataclass
class DeleteStats:
    files_deleted: int = 0
    folders_deleted: int = 0
    errors: int = 0
    total_files: int = 0


def _scan_folder_files(
    device: Any,
    folders: list[str],
    *,
    should_stop: ShouldStop = None,
) -> dict[str, list[PhoneFile]] | None:
    """Read each selected folder once and return files grouped by folder name."""
    folder_files: dict[str, list[PhoneFile]] = {}
    for folder_name in folders:
        if is_cancelled(should_stop):
            return None
        if not resolve_folder_path(device, folder_name):
            continue
        try:
            folder_files[folder_name] = list(iter_folder_files(device, folder_name))
        except OSError:
            folder_files[folder_name] = []
    return folder_files


def count_delete_files(device: Any, folders: list[str]) -> int:
    """Count all files in the selected folders before deletion."""
    scanned = _scan_folder_files(device, folders)
    if scanned is None:
        return 0
    return sum(len(files) for files in scanned.values())


def delete_folders(
    device: Any,
    folders: list[str],
    *,
    skip_trashed: bool,
    skip_thumbnails: bool,
    skip_screenshots: bool,
    should_stop: ShouldStop = None,
) -> Iterator[tuple[TransferEvent, DeleteStats]]:
    stats = DeleteStats()
    folder_files = _scan_folder_files(device, folders, should_stop=should_stop)
    if folder_files is None:
        yield TransferEvent(
            action="STOPPED",
            source="Delete stopped",
            reason="cancelled by user",
        ), stats
        return
    stats.total_files = sum(len(files) for files in folder_files.values())
    yield TransferEvent(
        action="QUEUE",
        source=f"{stats.total_files} file(s) queued for delete",
        reason=str(stats.total_files),
    ), stats

    for folder_name in folders:
        if is_cancelled(should_stop):
            yield TransferEvent(
                action="STOPPED",
                source="Delete stopped",
                reason="cancelled by user",
            ), stats
            return
        folder_path = resolve_folder_path(device, folder_name)
        if not folder_path:
            yield TransferEvent(
                action="PHASE",
                source=f"DCIM/{folder_name} — not present (skipped)",
            ), stats
            continue

        yield TransferEvent(action="PHASE", source=f"Deleting DCIM/{folder_name} ..."), stats

        for phone_file in folder_files.get(folder_name, []):
            if is_cancelled(should_stop):
                yield TransferEvent(
                    action="STOPPED",
                    source="Delete stopped",
                    reason="cancelled by user",
                ), stats
                return
            reason = filter_reason(
                phone_file.display_path,
                phone_file.filename,
                skip_trashed=skip_trashed,
                skip_thumbnails=skip_thumbnails,
                skip_screenshots=skip_screenshots,
            )
            try:
                content = win_access.get_content_from_device_path(device, phone_file.content_path)
                if content is None:
                    raise FileNotFoundError(phone_file.content_path)
                content.remove()
                stats.files_deleted += 1
                delete_reason = "filtered file removed before folder delete" if reason else ""
                yield TransferEvent(
                    action="DELETE",
                    source=phone_file.display_path,
                    reason=delete_reason,
                ), stats
            except Exception as exc:
                stats.errors += 1
                yield TransferEvent(
                    action="ERROR",
                    source=phone_file.display_path,
                    reason=f"delete failed: {exc}",
                ), stats

        try:
            folder = win_access.get_content_from_device_path(device, folder_path)
            if folder is not None:
                folder.remove()
            else:
                delete_folder(device, folder_name)
            stats.folders_deleted += 1
            yield TransferEvent(action="DELETE FOLDER", source=f"DCIM/{folder_name}"), stats
        except Exception as exc:
            stats.errors += 1
            yield TransferEvent(
                action="ERROR",
                source=f"DCIM/{folder_name}",
                reason=f"folder delete failed: {exc}",
            ), stats

    yield TransferEvent(
        action="SUMMARY",
        source=(
            f"Delete complete — {stats.folders_deleted} folder(s), "
            f"{stats.files_deleted} file(s), errors {stats.errors}"
        ),
    ), stats
