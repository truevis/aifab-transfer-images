"""Preview files that would be transferred without copying them."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from transfer.datetime_meta import resolve_capture_datetime
from transfer.filters import filter_reason
from transfer.locate import find_transferred_path
from transfer.mtp_client import PhoneFile, iter_folder_files
from transfer.rename import destination_path
from transfer.stop import ShouldStop, is_cancelled
from transfer.settings import TransferSettings


@dataclass(frozen=True)
class TransferCandidate:
    source_path: str
    original_name: str
    new_name: str
    dest_path: str
    already_exists: bool


def file_already_exists(
    dest_root: Path,
    filename: str,
    capture_dt,
    settings: TransferSettings,
) -> bool:
    existing = find_transferred_path(
        dest_root,
        filename,
        capture_dt,
        settings,
    )
    if existing is not None:
        return True

    dest = destination_path(
        dest_root,
        filename,
        capture_dt,
        rename_enabled=settings.rename_enabled,
        template=settings.rename_template,
        ext_lower=settings.ext_lower,
    )
    return dest.exists()


def list_transfer_candidates(
    device: Any,
    folders: list[str],
    settings: TransferSettings,
    *,
    should_stop: ShouldStop = None,
) -> list[TransferCandidate] | None:
    pending: list[PhoneFile] = []
    for folder_name in folders:
        if is_cancelled(should_stop):
            return None
        try:
            pending.extend(iter_folder_files(device, folder_name))
        except OSError:
            continue

    pending.sort(key=lambda phone_file: phone_file.date_modified, reverse=True)

    candidates: list[TransferCandidate] = []
    for phone_file in pending:
        if is_cancelled(should_stop):
            return None
        reason = filter_reason(
            phone_file.display_path,
            phone_file.filename,
            skip_trashed=settings.skip_trashed,
            skip_thumbnails=settings.skip_thumbnails,
            skip_screenshots=settings.skip_screenshots,
        )
        if reason:
            continue

        capture_dt = resolve_capture_datetime(
            phone_file.filename,
            fallback=phone_file.date_modified,
        )
        dest = destination_path(
            settings.dest_root,
            phone_file.filename,
            capture_dt,
            rename_enabled=settings.rename_enabled,
            template=settings.rename_template,
            ext_lower=settings.ext_lower,
        )
        already_exists = file_already_exists(
            settings.dest_root,
            phone_file.filename,
            capture_dt,
            settings,
        )

        candidates.append(
            TransferCandidate(
                source_path=phone_file.display_path,
                original_name=phone_file.filename,
                new_name=dest.name,
                dest_path=str(dest),
                already_exists=already_exists,
            )
        )

    return candidates
