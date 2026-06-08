"""Delete verified folders from the phone."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from transfer.events import TransferEvent
from transfer.filters import filter_reason
from transfer.mtp_client import delete_folder, iter_folder_files, resolve_folder_path
import mtp.win_access as win_access


@dataclass
class DeleteStats:
    files_deleted: int = 0
    folders_deleted: int = 0
    errors: int = 0


def delete_folders(
    device: Any,
    folders: list[str],
    *,
    skip_trashed: bool,
    skip_thumbnails: bool,
    skip_screenshots: bool,
) -> Iterator[tuple[TransferEvent, DeleteStats]]:
    stats = DeleteStats()

    for folder_name in folders:
        folder_path = resolve_folder_path(device, folder_name)
        if not folder_path:
            yield TransferEvent(
                action="PHASE",
                source=f"DCIM/{folder_name} — not present (skipped)",
            ), stats
            continue

        yield TransferEvent(action="PHASE", source=f"Deleting DCIM/{folder_name} ..."), stats

        for phone_file in iter_folder_files(device, folder_name):
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
