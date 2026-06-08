"""Shared transfer configuration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DEST = r"D:\Album-F"
DEFAULT_TEMPLATE = "$F-$Y-$M-$D_$H_$N_$S"
DEFAULT_FOLDERS = ("Camera", "OpenCamera", "Expert RAW")


@dataclass(frozen=True)
class TransferSettings:
    dest_root: Path
    rename_enabled: bool = True
    rename_template: str = DEFAULT_TEMPLATE
    ext_lower: bool = True
    skip_existing: bool = True
    skip_trashed: bool = True
    skip_thumbnails: bool = True
    skip_screenshots: bool = True


def settings_fingerprint(
    settings: TransferSettings,
    device_name: str,
    folders: list[str],
) -> str:
    payload = {
        "dest": str(settings.dest_root),
        "rename": settings.rename_enabled,
        "template": settings.rename_template,
        "ext_lower": settings.ext_lower,
        "skip_trashed": settings.skip_trashed,
        "skip_thumbnails": settings.skip_thumbnails,
        "skip_screenshots": settings.skip_screenshots,
        "folders": sorted(folders),
        "device": device_name,
    }
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
