"""Unit tests for transfer business logic."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from transfer.filters import (
    filter_reason,
    is_media_file,
    should_skip_screenshot,
    should_skip_thumbnail,
    should_skip_trashed,
)
from transfer.locate import find_transferred_path
from transfer.preview import list_transfer_candidates
from transfer.datetime_meta import capture_datetime_from_filename, resolve_capture_datetime
from transfer.filters import is_video_file
from transfer.rename import build_filename, destination_path, month_subfolder
from transfer.mtp_client import PhoneFile
from transfer.settings import DEFAULT_FOLDERS, DEFAULT_TEMPLATE, TransferSettings, settings_fingerprint


class TestRename(unittest.TestCase):
    def test_template_matches_reference_example(self) -> None:
        dt = datetime(2026, 6, 8, 19, 14, 27)
        result = build_filename(
            "IMG_1234.jpg",
            dt,
            rename_enabled=True,
            template=DEFAULT_TEMPLATE,
            ext_lower=True,
        )
        self.assertEqual(result, "IMG_1234-2026-06-08_19.14.27.jpg")

    def test_rename_disabled_keeps_original_stem(self) -> None:
        dt = datetime(2026, 6, 8, 19, 14, 27)
        result = build_filename("Photo.JPG", dt, rename_enabled=False, ext_lower=True)
        self.assertEqual(result, "Photo.jpg")

    def test_month_subfolder(self) -> None:
        dt = datetime(2026, 6, 8, 19, 14, 27)
        self.assertEqual(month_subfolder(dt), "2026-06")

    def test_destination_path(self) -> None:
        dt = datetime(2026, 6, 8, 19, 14, 27)
        dest = destination_path(
            Path(r"D:\Album-F"),
            "IMG_1234.jpg",
            dt,
            rename_enabled=True,
            template=DEFAULT_TEMPLATE,
            ext_lower=True,
        )
        self.assertEqual(
            dest,
            Path(r"D:\Album-F\2026-06\IMG_1234-2026-06-08_19.14.27.jpg"),
        )

    def test_video_template_matches_reference_example(self) -> None:
        dt = datetime(2023, 3, 1, 20, 2, 26)
        result = build_filename(
            "VID_20230301_200226.mp4",
            dt,
            rename_enabled=True,
            template=DEFAULT_TEMPLATE,
            ext_lower=True,
        )
        self.assertEqual(result, "VID_20230301_200226-2023-03-01_20.02.26.mp4")

    def test_capture_datetime_from_video_filename(self) -> None:
        parsed = capture_datetime_from_filename("VID_20230301_200226.mp4")
        self.assertEqual(parsed, datetime(2023, 3, 1, 20, 2, 26))

    def test_resolve_capture_datetime_prefers_video_filename(self) -> None:
        fallback = datetime(2020, 1, 1, 0, 0, 0)
        resolved = resolve_capture_datetime("VID_20230301_200226.mp4", fallback=fallback)
        self.assertEqual(resolved, datetime(2023, 3, 1, 20, 2, 26))


class TestFilters(unittest.TestCase):
    def test_media_extensions(self) -> None:
        self.assertTrue(is_media_file("photo.jpg"))
        self.assertTrue(is_media_file("clip.MP4"))
        self.assertFalse(is_media_file("notes.txt"))

    def test_video_extensions(self) -> None:
        self.assertTrue(is_video_file("clip.MP4"))
        self.assertFalse(is_video_file("photo.jpg"))

    def test_skip_trashed(self) -> None:
        self.assertTrue(should_skip_trashed("DCIM/Camera/.trashed-IMG.jpg", ".trashed-IMG.jpg"))
        self.assertTrue(should_skip_trashed("DCIM/Trash/IMG.jpg", "IMG.jpg"))

    def test_skip_thumbnails(self) -> None:
        self.assertTrue(should_skip_thumbnail("DCIM/.thumbnails/IMG.jpg"))
        self.assertTrue(should_skip_thumbnail("DCIM/Camera/cache/thumb.jpg"))

    def test_skip_screenshots(self) -> None:
        self.assertTrue(should_skip_screenshot("Pictures/Screenshots/s.png", "s.png"))
        self.assertTrue(should_skip_screenshot("DCIM/Camera/Screenshot_2026.png", "Screenshot_2026.png"))

    def test_importable_photo_passes_filters(self) -> None:
        reason = filter_reason(
            "DCIM/Camera/IMG_1234.jpg",
            "IMG_1234.jpg",
            skip_trashed=True,
            skip_thumbnails=True,
            skip_screenshots=True,
        )
        self.assertIsNone(reason)


class TestLocate(unittest.TestCase):
    def test_find_transferred_path_by_renamed_pattern(self) -> None:
        settings = TransferSettings(dest_root=Path("."))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            month_dir = root / "2026-06"
            month_dir.mkdir()
            transferred = month_dir / "IMG_1234-2026-06-08_19.14.27.jpg"
            transferred.write_bytes(b"test")
            found = find_transferred_path(
                root,
                "IMG_1234.jpg",
                datetime(2026, 5, 1, 12, 0, 0),
                settings,
            )
            self.assertEqual(found, transferred)


class TestPreview(unittest.TestCase):
    def test_list_transfer_candidates_applies_filters_and_rename(self) -> None:
        dt = datetime(2026, 6, 8, 19, 14, 27)
        phone_files = [
            PhoneFile(
                content_path="dev/storage/DCIM/Camera/IMG_1234.jpg",
                display_path="DCIM/Camera/IMG_1234.jpg",
                filename="IMG_1234.jpg",
                size=100,
                date_modified=dt,
                folder_path="dev/storage/DCIM/Camera",
            ),
            PhoneFile(
                content_path="dev/storage/DCIM/Camera/notes.txt",
                display_path="DCIM/Camera/notes.txt",
                filename="notes.txt",
                size=10,
                date_modified=dt,
                folder_path="dev/storage/DCIM/Camera",
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            settings = TransferSettings(dest_root=Path(tmp))
            with patch(
                "transfer.preview.iter_folder_files",
                side_effect=[phone_files, []],
            ):
                candidates = list_transfer_candidates(object(), ["Camera"], settings)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].original_name, "IMG_1234.jpg")
        self.assertEqual(candidates[0].new_name, "IMG_1234-2026-06-08_19.14.27.jpg")
        self.assertTrue(candidates[0].dest_path.endswith(
            r"2026-06\IMG_1234-2026-06-08_19.14.27.jpg"
        ) or candidates[0].dest_path.endswith(
            "2026-06/IMG_1234-2026-06-08_19.14.27.jpg"
        ))

    def test_list_transfer_candidates_skips_existing(self) -> None:
        dt = datetime(2026, 6, 8, 19, 14, 27)
        phone_files = [
            PhoneFile(
                content_path="dev/storage/DCIM/Camera/IMG_1234.jpg",
                display_path="DCIM/Camera/IMG_1234.jpg",
                filename="IMG_1234.jpg",
                size=100,
                date_modified=dt,
                folder_path="dev/storage/DCIM/Camera",
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = destination_path(
                root,
                "IMG_1234.jpg",
                dt,
                rename_enabled=True,
                template=DEFAULT_TEMPLATE,
                ext_lower=True,
            )
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"already there")

            settings = TransferSettings(dest_root=root, skip_existing=True)
            with patch(
                "transfer.preview.iter_folder_files",
                side_effect=[phone_files, []],
            ):
                candidates = list_transfer_candidates(object(), ["Camera"], settings)

        self.assertEqual(candidates, [])


class TestSettings(unittest.TestCase):
    def test_default_folders_include_expert_raw(self) -> None:
        self.assertIn("Expert RAW", DEFAULT_FOLDERS)
        self.assertIn("Camera", DEFAULT_FOLDERS)
        self.assertIn("OpenCamera", DEFAULT_FOLDERS)

    def test_fingerprint_changes_when_destination_changes(self) -> None:
        settings_a = TransferSettings(dest_root=Path(r"D:\Album-F"))
        settings_b = TransferSettings(dest_root=Path(r"D:\Album-G"))
        fp_a = settings_fingerprint(settings_a, "Pixel 7 Pro", ["Camera"])
        fp_b = settings_fingerprint(settings_b, "Pixel 7 Pro", ["Camera"])
        self.assertNotEqual(fp_a, fp_b)


if __name__ == "__main__":
    unittest.main()
