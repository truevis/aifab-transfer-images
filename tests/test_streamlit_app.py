"""Streamlit UI smoke tests."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from streamlit.testing.v1 import AppTest

from transfer.mtp_client import DeviceInfo


def _mock_devices() -> list[DeviceInfo]:
    return [
        DeviceInfo(
            index=0,
            name="Pixel 7 Pro",
            description="Pixel 7 Pro",
            serial="TESTSERIAL",
            devicename="Pixel 7 Pro_Pixel 7 Pro_TESTSERIAL",
        )
    ]


def _mock_dcim_folders(_device: object) -> list[str]:
    return ["Camera", "OpenCamera", "Expert RAW", "Facebook"]


@patch("transfer.mtp_client.close_device")
@patch("transfer.mtp_client.open_device", return_value=MagicMock())
@patch("transfer.mtp_client.list_dcim_folders", side_effect=_mock_dcim_folders)
@patch("transfer.mtp_client.list_devices", side_effect=_mock_devices)
class TestStreamlitApp(unittest.TestCase):
    def test_app_loads_and_renders_core_ui(
        self,
        _mock_list_devices,
        _mock_list_folders,
        _mock_open_device,
        _mock_close_device,
    ) -> None:
        at = AppTest.from_file("app.py", default_timeout=30)
        at.run()

        self.assertFalse(at.exception)
        titles = [t.value for t in at.title]
        self.assertIn("Import Photos and Videos", titles)

        labels = {w.label for w in at.text_input}
        labels.update(w.label for w in at.selectbox)
        labels.update(w.label for w in at.multiselect)
        labels.update(w.label for w in at.checkbox)
        labels.update(w.label for w in at.button)

        self.assertIn("Folder", labels)
        self.assertIn("DCIM folders", labels)
        self.assertIn("Skip existing files", labels)
        self.assertIn("Rename files", labels)

        button_labels = {b.label for b in at.button}
        self.assertIn("Start Import", button_labels)
        self.assertIn("Preview transfer list", button_labels)
        self.assertIn("Verify Transfer", button_labels)
        self.assertIn("Delete from Phone", button_labels)
        self.assertIn("Clear log", button_labels)

    def test_sidebar_contains_filter_defaults(
        self,
        _mock_list_devices,
        _mock_list_folders,
        _mock_open_device,
        _mock_close_device,
    ) -> None:
        at = AppTest.from_file("app.py", default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        checkbox_labels = {c.label for c in at.checkbox}
        self.assertIn("Skip trashed files", checkbox_labels)
        self.assertIn("Skip thumbnails", checkbox_labels)
        self.assertIn("Skip screenshots", checkbox_labels)

        skip_existing = next(c for c in at.checkbox if c.label == "Skip existing files")
        skip_trashed = next(c for c in at.checkbox if c.label == "Skip trashed files")
        rename_files = next(c for c in at.checkbox if c.label == "Rename files")
        self.assertTrue(skip_existing.value)
        self.assertTrue(skip_trashed.value)
        self.assertTrue(rename_files.value)

    def test_default_folders_include_expert_raw(
        self,
        _mock_list_devices,
        _mock_list_folders,
        _mock_open_device,
        _mock_close_device,
    ) -> None:
        at = AppTest.from_file("app.py", default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        folder_select = next(w for w in at.multiselect if w.label == "DCIM folders")
        self.assertIn("Camera", folder_select.value)
        self.assertIn("OpenCamera", folder_select.value)
        self.assertIn("Expert RAW", folder_select.value)
        self.assertNotIn("Facebook", folder_select.value)

    def test_delete_button_disabled_without_verify(
        self,
        _mock_list_devices,
        _mock_list_folders,
        _mock_open_device,
        _mock_close_device,
    ) -> None:
        at = AppTest.from_file("app.py", default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        delete_btn = next(b for b in at.button if b.label == "Delete from Phone")
        self.assertTrue(delete_btn.disabled)


if __name__ == "__main__":
    unittest.main()
