"""Rename template engine."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from transfer.settings import DEFAULT_TEMPLATE


def build_filename(
    original_name: str,
    capture_dt: datetime,
    *,
    rename_enabled: bool,
    template: str = DEFAULT_TEMPLATE,
    ext_lower: bool = True,
) -> str:
    stem = Path(original_name).stem
    ext = Path(original_name).suffix
    if ext_lower:
        ext = ext.lower()
    if not rename_enabled:
        return f"{stem}{ext}"

    result = template
    result = result.replace("$F", stem)
    result = result.replace("$Y", f"{capture_dt.year:04d}")
    result = result.replace("$M", f"{capture_dt.month:02d}")
    result = result.replace("$D", f"{capture_dt.day:02d}")
    result = result.replace("$H", f"{capture_dt.hour:02d}")
    result = result.replace("$N", f"{capture_dt.minute:02d}")
    result = result.replace("$S", f"{capture_dt.second:02d}")
    return f"{result}{ext}"


def month_subfolder(capture_dt: datetime) -> str:
    return f"{capture_dt.year:04d}-{capture_dt.month:02d}"


def destination_path(
    dest_root: Path,
    original_name: str,
    capture_dt: datetime,
    *,
    rename_enabled: bool,
    template: str = DEFAULT_TEMPLATE,
    ext_lower: bool = True,
) -> Path:
    filename = build_filename(
        original_name,
        capture_dt,
        rename_enabled=rename_enabled,
        template=template,
        ext_lower=ext_lower,
    )
    return dest_root / month_subfolder(capture_dt) / filename
