"""File filtering for phone media import."""

from __future__ import annotations

from pathlib import PurePosixPath

MEDIA_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".heic",
    ".heif",
    ".dng",
    ".raw",
    ".cr2",
    ".nef",
    ".arw",
    ".orf",
    ".rw2",
    ".mp4",
    ".mov",
    ".mkv",
    ".avi",
    ".3gp",
    ".m4v",
    ".webm",
}


VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".avi",
    ".3gp",
    ".m4v",
    ".webm",
}


def is_media_file(filename: str) -> bool:
    return PurePosixPath(filename).suffix.lower() in MEDIA_EXTENSIONS


def is_video_file(filename: str) -> bool:
    return PurePosixPath(filename).suffix.lower() in VIDEO_EXTENSIONS


def should_skip_trashed(path: str, filename: str) -> bool:
    lowered_path = path.replace("\\", "/").lower()
    lowered_name = filename.lower()
    if lowered_name.startswith(".trashed-"):
        return True
    if "/trash/" in lowered_path or "/.trash" in lowered_path:
        return True
    return False


def should_skip_thumbnail(path: str) -> bool:
    lowered = path.replace("\\", "/").lower()
    if ".thumbnails" in lowered:
        return True
    if "/cache/" in lowered:
        return True
    parts = lowered.split("/")
    return any("thumb" in part for part in parts)


def should_skip_screenshot(path: str, filename: str) -> bool:
    lowered_path = path.replace("\\", "/").lower()
    lowered_name = filename.lower()
    if "/pictures/screenshots" in lowered_path or "/screenshots/" in lowered_path:
        return True
    markers = ("screenshot", "scr_", "screen_")
    return any(marker in lowered_name for marker in markers)


def filter_reason(
    path: str,
    filename: str,
    *,
    skip_trashed: bool,
    skip_thumbnails: bool,
    skip_screenshots: bool,
) -> str | None:
    if not is_media_file(filename):
        return "not a media file"
    if skip_trashed and should_skip_trashed(path, filename):
        return "trashed filter"
    if skip_thumbnails and should_skip_thumbnail(path):
        return "thumbnail filter"
    if skip_screenshots and should_skip_screenshot(path, filename):
        return "screenshot filter"
    return None
