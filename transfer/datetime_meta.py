"""Capture datetime from EXIF, video filename, or file metadata."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from PIL import Image
from PIL.ExifTags import Base as ExifBase

from transfer.filters import is_video_file

_VIDEO_STEM_PATTERN = re.compile(r"_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$")


def capture_datetime_from_filename(filename: str) -> datetime | None:
    """Parse YYYYMMDD_HHMMSS from Android-style video names (e.g. VID_20230301_200226)."""
    match = _VIDEO_STEM_PATTERN.search(Path(filename).stem)
    if not match:
        return None
    year, month, day, hour, minute, second = (int(part) for part in match.groups())
    return datetime(year, month, day, hour, minute, second)


def resolve_capture_datetime(
    filename: str,
    *,
    file_path: Path | None = None,
    fallback: datetime | None = None,
) -> datetime:
    if is_video_file(filename):
        from_name = capture_datetime_from_filename(filename)
        if from_name is not None:
            return from_name
    elif file_path is not None:
        return capture_datetime_from_file(file_path, fallback=fallback)
    if fallback is not None:
        return fallback
    if file_path is not None:
        return datetime.fromtimestamp(file_path.stat().st_mtime)
    raise ValueError(f"Cannot resolve capture datetime for {filename!r}")


def capture_datetime_from_file(path: Path, fallback: datetime | None = None) -> datetime:
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            if exif:
                for tag_id, value in exif.items():
                    tag = ExifBase(tag_id)
                    if tag.name in {"DateTimeOriginal", "DateTime", "DateTimeDigitized"}:
                        parsed = _parse_exif_datetime(str(value))
                        if parsed:
                            return parsed
    except Exception:
        pass

    if fallback is not None:
        return fallback

    stat = path.stat()
    return datetime.fromtimestamp(stat.st_mtime)


def _parse_exif_datetime(value: str) -> datetime | None:
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None
