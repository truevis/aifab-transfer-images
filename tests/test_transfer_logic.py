"""Unit tests for transfer business logic."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

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
from transfer.errors import is_disk_full_error, is_resource_in_use_error
from transfer.events import TransferEvent
from transfer.importer import import_files
from transfer.filters import is_video_file
from transfer.rename import build_filename, destination_path, month_subfolder
from transfer.mtp_client import PhoneFile
from transfer.mtp_client import PhoneFile
from transfer.phone_cleanup import count_delete_files, delete_folders
from transfer.settings import DEFAULT_FOLDERS, DEFAULT_TEMPLATE, TransferSettings, settings_fingerprint
from transfer.verify import verify_transfer


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
        self.assertEqual(result, "IMG_1234-2026-06-08_19_14_27.jpg")

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
            Path(r"D:\Album-F\2026-06\IMG_1234-2026-06-08_19_14_27.jpg"),
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
        self.assertEqual(result, "VID_20230301_200226-2023-03-01_20_02_26.mp4")

    def test_capture_datetime_from_video_filename(self) -> None:
        parsed = capture_datetime_from_filename("VID_20230301_200226.mp4")
        self.assertEqual(parsed, datetime(2023, 3, 1, 20, 2, 26))

    def test_capture_datetime_from_compact_video_filename(self) -> None:
        parsed = capture_datetime_from_filename("20260508_162550.mp4")
        self.assertEqual(parsed, datetime(2026, 5, 8, 16, 25, 50))

    def test_compact_video_template_matches_reference_example(self) -> None:
        dt = datetime(2026, 5, 8, 16, 25, 50)
        result = build_filename(
            "20260508_162550.mp4",
            dt,
            rename_enabled=True,
            template=DEFAULT_TEMPLATE,
            ext_lower=True,
        )
        self.assertEqual(result, "20260508_162550-2026-05-08_16_25_50.mp4")

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
            transferred = month_dir / "IMG_1234-2026-06-08_19_14_27.jpg"
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
        self.assertEqual(candidates[0].new_name, "IMG_1234-2026-06-08_19_14_27.jpg")
        self.assertFalse(candidates[0].already_exists)
        self.assertTrue(candidates[0].dest_path.endswith(
            r"2026-06\IMG_1234-2026-06-08_19_14_27.jpg"
        ) or candidates[0].dest_path.endswith(
            "2026-06/IMG_1234-2026-06-08_19_14_27.jpg"
        ))

    def test_list_transfer_candidates_marks_existing_files(self) -> None:
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

        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0].already_exists)


class TestResourceInUse(unittest.TestCase):
    def test_is_resource_in_use_error_detects_message(self) -> None:
        exc = OSError("Error getting file': The requested resource is in use.")
        self.assertTrue(is_resource_in_use_error(exc))

    def test_import_retries_once_on_resource_in_use(self) -> None:
        dt = datetime(2026, 6, 8, 19, 14, 27)
        phone_files = [
            PhoneFile(
                content_path="dev/storage/DCIM/Camera/IMG_0001.jpg",
                display_path="DCIM/Camera/IMG_0001.jpg",
                filename="IMG_0001.jpg",
                size=100,
                date_modified=dt,
                folder_path="dev/storage/DCIM/Camera",
            ),
        ]
        calls = {"count": 0}
        in_use = OSError("Error getting file': The requested resource is in use.")

        def fake_download(_device, _content_path, dest_path) -> None:
            calls["count"] += 1
            if calls["count"] == 1:
                raise in_use
            Path(dest_path).write_bytes(b"ok")

        with tempfile.TemporaryDirectory() as tmp:
            settings = TransferSettings(dest_root=Path(tmp))
            with (
                patch("transfer.importer.mtp_path", return_value="dev/storage/DCIM/Camera"),
                patch("transfer.importer.iter_folder_files", return_value=phone_files),
                patch("transfer.importer.download_to_path", side_effect=fake_download),
                patch("transfer.importer.time.sleep"),
            ):
                events = [
                    event
                    for event, _stats in import_files(object(), ["Camera"], settings)
                ]

        actions = [event.action for event in events]
        self.assertEqual(calls["count"], 2)
        self.assertIn("RETRY", actions)
        self.assertIn("COPY", actions)
        self.assertNotIn("ERROR", actions)

    def test_import_fails_after_retry_exhausted(self) -> None:
        dt = datetime(2026, 6, 8, 19, 14, 27)
        phone_files = [
            PhoneFile(
                content_path="dev/storage/DCIM/Camera/IMG_0001.jpg",
                display_path="DCIM/Camera/IMG_0001.jpg",
                filename="IMG_0001.jpg",
                size=100,
                date_modified=dt,
                folder_path="dev/storage/DCIM/Camera",
            ),
        ]
        in_use = OSError("Error getting file': The requested resource is in use.")

        with tempfile.TemporaryDirectory() as tmp:
            settings = TransferSettings(dest_root=Path(tmp))
            with (
                patch("transfer.importer.mtp_path", return_value="dev/storage/DCIM/Camera"),
                patch("transfer.importer.iter_folder_files", return_value=phone_files),
                patch("transfer.importer.download_to_path", side_effect=in_use),
                patch("transfer.importer.time.sleep"),
            ):
                events = [
                    event
                    for event, _stats in import_files(object(), ["Camera"], settings)
                ]

        actions = [event.action for event in events]
        self.assertIn("RETRY", actions)
        self.assertIn("ERROR", actions)
        self.assertNotIn("COPY", actions)


class TestDiskFull(unittest.TestCase):
    def test_is_disk_full_error_detects_errno_28(self) -> None:
        self.assertTrue(is_disk_full_error(OSError(28, "No space left on device")))

    def test_is_disk_full_error_detects_message(self) -> None:
        self.assertTrue(is_disk_full_error(OSError("not enough space on the disk")))

    def test_import_stops_after_disk_full_error(self) -> None:
        dt = datetime(2026, 6, 8, 19, 14, 27)
        phone_files = [
            PhoneFile(
                content_path="dev/storage/DCIM/Camera/IMG_0001.jpg",
                display_path="DCIM/Camera/IMG_0001.jpg",
                filename="IMG_0001.jpg",
                size=100,
                date_modified=dt,
                folder_path="dev/storage/DCIM/Camera",
            ),
            PhoneFile(
                content_path="dev/storage/DCIM/Camera/IMG_0002.jpg",
                display_path="DCIM/Camera/IMG_0002.jpg",
                filename="IMG_0002.jpg",
                size=100,
                date_modified=dt,
                folder_path="dev/storage/DCIM/Camera",
            ),
        ]

        def fake_download(_device, _content_path, _dest_path) -> None:
            raise OSError(28, "No space left on device")

        with tempfile.TemporaryDirectory() as tmp:
            settings = TransferSettings(dest_root=Path(tmp))
            with (
                patch("transfer.importer.mtp_path", return_value="dev/storage/DCIM/Camera"),
                patch("transfer.importer.iter_folder_files", return_value=phone_files),
                patch("transfer.importer.download_to_path", side_effect=fake_download),
            ):
                events = [
                    event
                    for event, _stats in import_files(object(), ["Camera"], settings)
                ]

        actions = [event.action for event in events]
        self.assertIn("STOPPED", actions)
        self.assertEqual(actions.count("ERROR"), 1)
        self.assertEqual(events[-1].action, "SUMMARY")
        self.assertIn("target drive is full", events[-1].source)


class TestEvents(unittest.TestCase):
    def test_error_includes_planned_destination(self) -> None:
        event = TransferEvent(
            action="ERROR",
            source="DCIM/Camera/20260508_162550.mp4",
            dest=r"D:\Album-F\2026-05\20260508_162550-2026-05-08_16_25_50.mp4",
            reason="download failed: No space left on device",
        )
        formatted = event.format_line()
        self.assertIn("20260508_162550-2026-05-08_16_25_50.mp4", formatted)
        self.assertIn("download failed", formatted)


class TestDelete(unittest.TestCase):
    def test_count_delete_files_sums_selected_folders(self) -> None:
        dt = datetime(2026, 6, 8, 19, 14, 27)
        camera_files = [
            PhoneFile(
                content_path="dev/storage/DCIM/Camera/a.jpg",
                display_path="DCIM/Camera/a.jpg",
                filename="a.jpg",
                size=1,
                date_modified=dt,
                folder_path="dev/storage/DCIM/Camera",
            ),
            PhoneFile(
                content_path="dev/storage/DCIM/Camera/b.jpg",
                display_path="DCIM/Camera/b.jpg",
                filename="b.jpg",
                size=1,
                date_modified=dt,
                folder_path="dev/storage/DCIM/Camera",
            ),
        ]

        with (
            patch(
                "transfer.phone_cleanup.resolve_folder_path",
                side_effect=lambda _device, folder: f"path/{folder}" if folder == "Camera" else None,
            ),
            patch(
                "transfer.phone_cleanup.iter_folder_files",
                return_value=camera_files,
            ),
        ):
            total = count_delete_files(object(), ["Camera", "Missing"])

        self.assertEqual(total, 2)

    def test_delete_folders_emits_queue_with_total(self) -> None:
        dt = datetime(2026, 6, 8, 19, 14, 27)
        camera_files = [
            PhoneFile(
                content_path="dev/storage/DCIM/Camera/a.jpg",
                display_path="DCIM/Camera/a.jpg",
                filename="a.jpg",
                size=1,
                date_modified=dt,
                folder_path="dev/storage/DCIM/Camera",
            ),
        ]
        mock_content = MagicMock()

        with (
            patch(
                "transfer.phone_cleanup.resolve_folder_path",
                return_value="path/Camera",
            ),
            patch(
                "transfer.phone_cleanup.iter_folder_files",
                return_value=camera_files,
            ),
            patch(
                "transfer.phone_cleanup.win_access.get_content_from_device_path",
                return_value=mock_content,
            ),
        ):
            events = [
                event
                for event, _stats in delete_folders(
                    object(),
                    ["Camera"],
                    skip_trashed=True,
                    skip_thumbnails=True,
                    skip_screenshots=True,
                )
            ]

        queue = next(event for event in events if event.action == "QUEUE")
        self.assertEqual(queue.reason, "1")
        self.assertEqual(events[-1].action, "SUMMARY")


class TestImportStop(unittest.TestCase):
    def test_import_stops_when_should_stop_requested(self) -> None:
        dt = datetime(2026, 6, 8, 19, 14, 27)
        phone_files = [
            PhoneFile(
                content_path="dev/storage/DCIM/Camera/IMG_0001.jpg",
                display_path="DCIM/Camera/IMG_0001.jpg",
                filename="IMG_0001.jpg",
                size=100,
                date_modified=dt,
                folder_path="dev/storage/DCIM/Camera",
            ),
            PhoneFile(
                content_path="dev/storage/DCIM/Camera/IMG_0002.jpg",
                display_path="DCIM/Camera/IMG_0002.jpg",
                filename="IMG_0002.jpg",
                size=100,
                date_modified=dt,
                folder_path="dev/storage/DCIM/Camera",
            ),
        ]
        calls = {"count": 0}

        def should_stop() -> bool:
            calls["count"] += 1
            return calls["count"] >= 2

        with tempfile.TemporaryDirectory() as tmp:
            settings = TransferSettings(dest_root=Path(tmp))
            with (
                patch("transfer.importer.mtp_path", return_value="dev/storage/DCIM/Camera"),
                patch("transfer.importer.iter_folder_files", return_value=phone_files),
                patch("transfer.importer.download_to_path"),
            ):
                events = [
                    event
                    for event, _stats in import_files(
                        object(),
                        ["Camera"],
                        settings,
                        should_stop=should_stop,
                    )
                ]

        actions = [event.action for event in events]
        self.assertIn("STOPPED", actions)
        self.assertEqual(events[-1].action, "SUMMARY")
        self.assertIn("stopped by user", events[-1].source.lower())


class TestVerify(unittest.TestCase):
    def test_verify_ready_when_file_exists_despite_size_mismatch(self) -> None:
        dt = datetime(2026, 6, 8, 19, 14, 27)
        phone_files = [
            PhoneFile(
                content_path="dev/storage/DCIM/Camera/IMG_1234.jpg",
                display_path="DCIM/Camera/IMG_1234.jpg",
                filename="IMG_1234.jpg",
                size=999,
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
            dest.write_bytes(b"on disk")

            settings = TransferSettings(dest_root=root)
            with patch(
                "transfer.verify.iter_folder_files",
                return_value=phone_files,
            ):
                events = list(
                    verify_transfer(object(), ["Camera"], settings, "fp")
                )

        result = next(event for event in events if event.action == "_RESULT")
        self.assertEqual(result.source, "ready")
        actions = [event.action for event in events]
        self.assertIn("WARN", actions)
        self.assertNotIn("FAIL", actions)

    def test_verify_blocked_when_file_missing(self) -> None:
        dt = datetime(2026, 6, 8, 19, 14, 27)
        phone_files = [
            PhoneFile(
                content_path="dev/storage/DCIM/Camera/IMG_9999.jpg",
                display_path="DCIM/Camera/IMG_9999.jpg",
                filename="IMG_9999.jpg",
                size=100,
                date_modified=dt,
                folder_path="dev/storage/DCIM/Camera",
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            settings = TransferSettings(dest_root=Path(tmp))
            with patch(
                "transfer.verify.iter_folder_files",
                return_value=phone_files,
            ):
                events = list(
                    verify_transfer(object(), ["Camera"], settings, "fp")
                )

        result = next(event for event in events if event.action == "_RESULT")
        self.assertEqual(result.source, "blocked")
        self.assertEqual(result.reason, "1")


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
