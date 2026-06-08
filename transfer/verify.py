"""Verify transferred files exist on destination before phone cleanup."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from transfer.datetime_meta import resolve_capture_datetime
from transfer.stop import ShouldStop, is_cancelled
from transfer.events import TransferEvent
from transfer.filters import filter_reason
from transfer.mtp_client import iter_folder_files
from transfer.locate import find_transferred_path
from transfer.rename import destination_path
from transfer.settings import TransferSettings


@dataclass
class VerifyResult:
    status: str
    missing_count: int = 0
    verified_count: int = 0
    error_count: int = 0
    fingerprint: str = ""


def verify_transfer(
    device: Any,
    folders: list[str],
    settings: TransferSettings,
    fingerprint: str,
    *,
    should_stop: ShouldStop = None,
) -> Iterator[TransferEvent]:
    missing_count = 0
    mismatch_count = 0
    verified_count = 0
    error_count = 0

    for folder_name in folders:
        if is_cancelled(should_stop):
            yield TransferEvent(action="STOPPED", source="Verify stopped", reason="cancelled by user")
            yield TransferEvent(action="_RESULT", source="blocked", reason=str(missing_count))
            return
        folder_files = list(iter_folder_files(device, folder_name))
        if not folder_files:
            yield TransferEvent(
                action="VERIFY",
                source=f"DCIM/{folder_name} — folder not present or empty (skipped)",
            )
            continue

        folder_ok = 0
        importable_count = 0
        for phone_file in folder_files:
            if is_cancelled(should_stop):
                yield TransferEvent(action="STOPPED", source="Verify stopped", reason="cancelled by user")
                yield TransferEvent(action="_RESULT", source="blocked", reason=str(missing_count))
                return
            reason = filter_reason(
                phone_file.display_path,
                phone_file.filename,
                skip_trashed=settings.skip_trashed,
                skip_thumbnails=settings.skip_thumbnails,
                skip_screenshots=settings.skip_screenshots,
            )
            if reason:
                continue

            importable_count += 1
            capture_dt = resolve_capture_datetime(
                phone_file.filename,
                fallback=phone_file.date_modified,
            )
            dest = find_transferred_path(
                settings.dest_root,
                phone_file.filename,
                capture_dt,
                settings,
            )
            if dest is None:
                dest = destination_path(
                    settings.dest_root,
                    phone_file.filename,
                    capture_dt,
                    rename_enabled=settings.rename_enabled,
                    template=settings.rename_template,
                    ext_lower=settings.ext_lower,
                )
                if not dest.exists():
                    dest = None

            if dest is None:
                missing_count += 1
                yield TransferEvent(
                    action="FAIL",
                    source=phone_file.display_path,
                    reason="missing at destination",
                )
                continue

            if not _size_matches(dest, phone_file.size):
                mismatch_count += 1
                yield TransferEvent(
                    action="WARN",
                    source=phone_file.display_path,
                    dest=str(dest),
                    reason="size mismatch at destination (file found)",
                )

            folder_ok += 1
            verified_count += 1

        yield TransferEvent(
            action="VERIFY",
            source=f"DCIM/{folder_name} — {folder_ok}/{importable_count} files OK",
        )

    if missing_count == 0 and error_count == 0:
        if mismatch_count:
            ready_msg = (
                f"All importable files found at {settings.dest_root} "
                f"({mismatch_count} size mismatch warning(s))"
            )
        else:
            ready_msg = f"All importable files verified at {settings.dest_root}"
        yield TransferEvent(action="READY", source=ready_msg)
        status = "ready"
    else:
        yield TransferEvent(
            action="BLOCKED",
            source=f"Delete disabled: {missing_count} file(s) not yet transferred",
        )
        status = "blocked"

    yield TransferEvent(
        action="_RESULT",
        source=status,
        reason=str(missing_count),
    )


def _size_matches(dest: Path, source_size: int) -> bool:
    if source_size < 0:
        return True
    try:
        return dest.stat().st_size == source_size
    except OSError:
        return False
