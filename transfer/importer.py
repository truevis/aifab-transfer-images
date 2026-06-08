"""Import files from phone to local destination."""

from __future__ import annotations

import shutil
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from transfer.datetime_meta import resolve_capture_datetime
from transfer.errors import DiskFullError, is_disk_full_error, is_resource_in_use_error
from transfer.events import TransferEvent
from transfer.filters import filter_reason
from transfer.mtp_client import PhoneFile, download_to_path, iter_folder_files, mtp_path
from transfer.locate import find_transferred_path
from transfer.rename import destination_path
from transfer.stop import ShouldStop, is_cancelled
from transfer.settings import TransferSettings


RESOURCE_IN_USE_RETRY_DELAY_SEC = 1.5
RESOURCE_IN_USE_MAX_ATTEMPTS = 2


@dataclass
class ImportStats:
    scanned: int = 0
    copied: int = 0
    skipped_existing: int = 0
    skipped_filter: int = 0
    errors: int = 0


def import_files(
    device: Any,
    folders: list[str],
    settings: TransferSettings,
    *,
    should_stop: ShouldStop = None,
) -> Iterator[tuple[TransferEvent, ImportStats]]:
    stats = ImportStats()

    yield TransferEvent(action="PHASE", source=f"Starting import to {settings.dest_root}"), stats

    pending: list[PhoneFile] = []
    for folder_name in folders:
        if is_cancelled(should_stop):
            yield TransferEvent(
                action="STOPPED",
                source="Import stopped",
                reason="cancelled by user",
            ), stats
            return
        try:
            scan_path = mtp_path(device, "DCIM", folder_name)
        except OSError as exc:
            stats.errors += 1
            yield TransferEvent(
                action="ERROR",
                source=f"DCIM/{folder_name}",
                reason=str(exc),
            ), stats
            continue

        yield TransferEvent(
            action="PHASE",
            source=f"Scanning {scan_path} ...",
        ), stats

        try:
            folder_files = list(iter_folder_files(device, folder_name))
        except OSError as exc:
            stats.errors += 1
            yield TransferEvent(
                action="ERROR",
                source=f"DCIM/{folder_name}",
                reason=str(exc),
            ), stats
            continue

        if not folder_files:
            yield TransferEvent(
                action="PHASE",
                source=f"DCIM/{folder_name} — no files found (folder may be missing)",
            ), stats
            continue

        yield TransferEvent(
            action="PHASE",
            source=f"DCIM/{folder_name} — found {len(folder_files)} file(s)",
        ), stats
        pending.extend(folder_files)

    pending.sort(key=lambda phone_file: phone_file.date_modified, reverse=True)

    yield TransferEvent(
        action="QUEUE",
        source=f"{len(pending)} file(s) queued for import",
        reason=str(len(pending)),
    ), stats

    stopped_reason = ""
    user_cancelled = False
    try:
        for phone_file in pending:
            if is_cancelled(should_stop):
                user_cancelled = True
                stopped_reason = "cancelled by user"
                yield TransferEvent(
                    action="STOPPED",
                    source="Import stopped",
                    reason=stopped_reason,
                ), stats
                break
            stats.scanned += 1
            yield from _process_file(device, phone_file, settings, stats)
    except DiskFullError as exc:
        stopped_reason = str(exc)
        yield TransferEvent(
            action="STOPPED",
            source="Import stopped",
            reason=stopped_reason,
        ), stats

    if user_cancelled:
        summary = (
            f"Import stopped by user — copied {stats.copied}, "
            f"skipped existing {stats.skipped_existing}, "
            f"skipped filter {stats.skipped_filter}, errors {stats.errors}"
        )
    elif stopped_reason:
        summary = (
            f"Import stopped — target drive is full. "
            f"Copied {stats.copied}, skipped existing {stats.skipped_existing}, "
            f"skipped filter {stats.skipped_filter}, errors {stats.errors}"
        )
    else:
        summary = (
            f"Import complete — copied {stats.copied}, "
            f"skipped existing {stats.skipped_existing}, "
            f"skipped filter {stats.skipped_filter}, "
            f"errors {stats.errors}"
        )
    yield TransferEvent(action="SUMMARY", source=summary), stats


def _process_file(
    device: Any,
    phone_file: PhoneFile,
    settings: TransferSettings,
    stats: ImportStats,
) -> Iterator[tuple[TransferEvent, ImportStats]]:
    reason = filter_reason(
        phone_file.display_path,
        phone_file.filename,
        skip_trashed=settings.skip_trashed,
        skip_thumbnails=settings.skip_thumbnails,
        skip_screenshots=settings.skip_screenshots,
    )
    if reason:
        stats.skipped_filter += 1
        yield TransferEvent(
            action="SKIP",
            source=phone_file.display_path,
            reason=reason,
        ), stats
        return

    capture_dt = resolve_capture_datetime(
        phone_file.filename,
        fallback=phone_file.date_modified,
    )
    if settings.skip_existing:
        existing = find_transferred_path(
            settings.dest_root,
            phone_file.filename,
            capture_dt,
            settings,
        )
        if existing is not None:
            stats.skipped_existing += 1
            yield TransferEvent(
                action="SKIP",
                source=phone_file.display_path,
                dest=str(existing),
                reason="already exists at destination",
            ), stats
            return

    dest = destination_path(
        settings.dest_root,
        phone_file.filename,
        capture_dt,
        rename_enabled=settings.rename_enabled,
        template=settings.rename_template,
        ext_lower=settings.ext_lower,
    )

    if settings.skip_existing and dest.exists():
        stats.skipped_existing += 1
        yield TransferEvent(
            action="SKIP",
            source=phone_file.display_path,
            dest=str(dest),
            reason="already exists at destination",
        ), stats
        return

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(delete=False, dir=dest.parent) as tmp:
            temp_path = Path(tmp.name)

        for attempt in range(RESOURCE_IN_USE_MAX_ATTEMPTS):
            try:
                download_to_path(device, phone_file.content_path, temp_path)
                break
            except Exception as exc:
                if (
                    attempt + 1 < RESOURCE_IN_USE_MAX_ATTEMPTS
                    and is_resource_in_use_error(exc)
                ):
                    yield TransferEvent(
                        action="RETRY",
                        source=phone_file.display_path,
                        reason="File in use on phone, retrying once...",
                    ), stats
                    time.sleep(RESOURCE_IN_USE_RETRY_DELAY_SEC)
                    continue
                raise
        capture_dt = resolve_capture_datetime(
            phone_file.filename,
            file_path=temp_path,
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

        if settings.skip_existing and dest.exists():
            temp_path.unlink(missing_ok=True)
            stats.skipped_existing += 1
            yield TransferEvent(
                action="SKIP",
                source=phone_file.display_path,
                dest=str(dest),
                reason="already exists at destination",
            ), stats
            return

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(temp_path), dest)
        stats.copied += 1
        yield TransferEvent(
            action="COPY",
            source=phone_file.display_path,
            dest=str(dest),
        ), stats
    except Exception as exc:
        stats.errors += 1
        if "temp_path" in locals() and temp_path.exists():
            temp_path.unlink(missing_ok=True)
        reason = f"download failed: {exc}"
        yield TransferEvent(
            action="ERROR",
            source=phone_file.display_path,
            dest=str(dest),
            reason=reason,
        ), stats
        if is_disk_full_error(exc):
            raise DiskFullError(reason) from exc
