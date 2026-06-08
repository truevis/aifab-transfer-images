"""Locate transferred files on disk."""

from __future__ import annotations

from pathlib import Path

from transfer.datetime_meta import resolve_capture_datetime
from transfer.rename import destination_path
from transfer.settings import TransferSettings


def find_transferred_path(
    dest_root: Path,
    filename: str,
    fallback_dt,
    settings: TransferSettings,
) -> Path | None:
    capture_dt = resolve_capture_datetime(filename, fallback=fallback_dt)
    primary = destination_path(
        dest_root,
        filename,
        capture_dt,
        rename_enabled=settings.rename_enabled,
        template=settings.rename_template,
        ext_lower=settings.ext_lower,
    )
    if primary.exists():
        return primary

    if not dest_root.exists():
        return None

    stem = Path(filename).stem
    ext = Path(filename).suffix.lower() if settings.ext_lower else Path(filename).suffix

    for month_dir in dest_root.iterdir():
        if not month_dir.is_dir():
            continue
        if settings.rename_enabled:
            matches = sorted(month_dir.glob(f"{stem}-*{ext}"))
        else:
            candidate = month_dir / f"{stem}{ext}"
            matches = [candidate] if candidate.exists() else []
        if matches:
            return matches[0]

    return None
