"""Streamlit UI smoke tests."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from streamlit.testing.v1 import AppTest

import app as streamlit_app
from transfer.events import TransferEvent
from transfer.importer import ImportStats
from transfer.mtp_client import DeviceInfo
from transfer.operation_control import OperationControl
from transfer.phone_cleanup import DeleteStats
from transfer.settings import TransferSettings, settings_fingerprint

_APP_FILE = str(Path(__file__).resolve().parents[1] / "app.py")


class _SessionState(dict):
    """Minimal session_state stand-in supporting both attr and mapping access."""

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: object) -> None:
        self[name] = value


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


def _sample_settings() -> TransferSettings:
    return TransferSettings(dest_root=Path("D:/Album-F"))


class TestWorkerOpenDeviceFailure(unittest.TestCase):
    def test_workers_finalize_when_open_device_fails(self) -> None:
        settings = _sample_settings()
        folders = ["Camera"]
        workers = (
            (
                streamlit_app._import_worker,
                (0, folders, settings, "Pixel"),
            ),
            (
                streamlit_app._verify_worker,
                (0, folders, settings, "Pixel"),
            ),
            (
                streamlit_app._delete_worker,
                (0, folders, settings),
            ),
            (
                streamlit_app._preview_worker,
                (0, folders, settings, "Pixel"),
            ),
        )
        for worker, args in workers:
            with self.subTest(worker=worker.__name__):
                op = OperationControl(kind=worker.__name__)
                with patch(
                    "app.open_device",
                    side_effect=RuntimeError("connection refused"),
                ):
                    worker(op, *args)
                self.assertTrue(op.finished)
                self.assertFalse(op.running)
                self.assertIsNotNone(op.error)


class TestImportCancelInvalidatesVerify(unittest.TestCase):
    def test_import_worker_cancel_marks_verify_stale(self) -> None:
        settings = _sample_settings()
        op = OperationControl(kind="_import_worker")
        events = [
            (
                TransferEvent(
                    action="STOPPED",
                    source="Import stopped",
                    reason="cancelled by user",
                ),
                ImportStats(scanned=1),
            ),
            (
                TransferEvent(
                    action="SUMMARY",
                    source="Import stopped by user — copied 0",
                ),
                ImportStats(scanned=1),
            ),
        ]
        with (
            patch("app.open_device", return_value=MagicMock()),
            patch("app.close_device"),
            patch("app.import_files", return_value=iter(events)),
        ):
            streamlit_app._import_worker(op, 0, ["Camera"], settings, "Pixel")
        self.assertTrue(op.invalidate_verify)
        self.assertTrue(op.finished)
        self.assertFalse(op.running)
        self.assertIsNone(op.verify_status)


class TestFinishOperationRerun(unittest.TestCase):
    def test_finish_operation_if_done_finalizes_and_signals_rerun(self) -> None:
        op = OperationControl(kind="import")
        op.finished = True
        op.running = False
        op.activity = {"mode": "idle", "label": "Import stopped"}
        session = SimpleNamespace(
            activity={},
            import_stats=ImportStats(),
            delete_stats=DeleteStats(),
            transfer_log=[],
            verify_status="ready",
            verify_fingerprint="old",
            verify_missing_count=0,
            transfer_candidates=[],
            preview_fingerprint="",
            operation_control=op,
            delete_confirm=True,
        )
        op.invalidate_verify = True
        with patch.object(streamlit_app.st, "session_state", session):
            needs_rerun = streamlit_app._finish_operation_if_done(op)
        self.assertTrue(needs_rerun)
        self.assertIsNone(session.verify_status)
        self.assertEqual(session.verify_fingerprint, "")
        self.assertIsNone(session.operation_control)

    def test_finish_operation_if_done_noop_while_running(self) -> None:
        op = OperationControl(kind="import")
        op.finished = False
        with patch.object(streamlit_app.st, "session_state", SimpleNamespace()):
            self.assertFalse(streamlit_app._finish_operation_if_done(op))


class TestClaimActivitySlots(unittest.TestCase):
    def test_claim_writes_each_outside_slot(self) -> None:
        """Full-app claim must touch every outside empty before fragment reruns."""
        badge, detail, progress, metrics = (
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )
        streamlit_app._claim_activity_slots(badge, detail, progress, metrics)
        badge.empty.assert_called_once_with()
        detail.empty.assert_called_once_with()
        progress.empty.assert_called_once_with()
        metrics.empty.assert_called_once_with()

    def test_poll_paints_when_operation_control_is_none(self) -> None:
        """Idle full-app path must still write outside slots (no early return)."""
        session = _SessionState(
            operation_control=None,
            activity=dict(streamlit_app._default_activity()),
            import_stats=ImportStats(),
            delete_stats=DeleteStats(),
        )
        badge, detail, progress, metrics = (
            MagicMock(),
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )
        settings = _sample_settings()
        with (
            patch.object(streamlit_app.st, "session_state", session),
            patch.object(streamlit_app, "_render_operation_actions") as render_actions,
            patch.object(streamlit_app, "_paint_activity") as paint,
        ):
            finished = streamlit_app._poll_operation_and_actions(
                badge,
                detail,
                progress,
                metrics,
                0,
                ["Camera"],
                settings,
                "Pixel",
            )
        self.assertFalse(finished)
        paint.assert_called_once_with(badge, detail, progress, metrics)
        render_actions.assert_called_once()


class TestOperationRunningAfterStop(unittest.TestCase):
    def test_finished_worker_does_not_keep_actions_disabled(self) -> None:
        """Stop Import full-reruns while running=True; after the worker
        finishes, action buttons must not stay gated on a stale op."""
        op = OperationControl(kind="_import_worker")
        op.running = False
        op.finished = True
        session = _SessionState(operation_control=op)
        with patch.object(streamlit_app.st, "session_state", session):
            self.assertFalse(streamlit_app._operation_running())

    def test_running_worker_keeps_actions_disabled(self) -> None:
        op = OperationControl(kind="_import_worker")
        session = _SessionState(operation_control=op)
        with patch.object(streamlit_app.st, "session_state", session):
            self.assertTrue(streamlit_app._operation_running())

    def test_fragment_only_rerun_requests_app_scope(self) -> None:
        """After a fragment-only finish, refresh outside metrics with scope=app."""
        op = OperationControl(kind="_import_worker")
        op.finished = True
        op.running = False
        op.activity = {"mode": "idle", "label": "Import stopped"}
        session = _SessionState(
            operation_control=op,
            activity=dict(streamlit_app._default_activity()),
            import_stats=ImportStats(),
            delete_stats=DeleteStats(),
            transfer_log=[],
            verify_status=None,
            verify_fingerprint="",
            verify_missing_count=0,
            transfer_candidates=[],
            preview_fingerprint="",
            operation_error=None,
            delete_confirm=False,
        )
        settings = _sample_settings()
        with (
            patch.object(streamlit_app.st, "session_state", session),
            patch.object(streamlit_app, "_fragment_only_run", return_value=True),
            patch.object(streamlit_app, "_render_operation_actions") as render_actions,
            patch.object(streamlit_app, "_paint_activity"),
            patch.object(streamlit_app.st, "rerun") as rerun,
        ):
            finished = streamlit_app._poll_operation_and_actions(
                MagicMock(),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                0,
                ["Camera"],
                settings,
                "Pixel",
            )
            if finished and streamlit_app._fragment_only_run():
                streamlit_app.st.rerun(scope="app")
        self.assertTrue(finished)
        render_actions.assert_called_once()
        rerun.assert_called_once_with(scope="app")
        self.assertIsNone(session.operation_control)

    def test_full_app_finish_does_not_rerun_loop(self) -> None:
        op = OperationControl(kind="_import_worker")
        op.finished = True
        op.running = False
        op.activity = {"mode": "idle", "label": "Import stopped"}
        session = _SessionState(
            operation_control=op,
            activity=dict(streamlit_app._default_activity()),
            import_stats=ImportStats(),
            delete_stats=DeleteStats(),
            transfer_log=[],
            verify_status=None,
            verify_fingerprint="",
            verify_missing_count=0,
            transfer_candidates=[],
            preview_fingerprint="",
            operation_error=None,
            delete_confirm=False,
        )
        settings = _sample_settings()
        with (
            patch.object(streamlit_app.st, "session_state", session),
            patch.object(streamlit_app, "_fragment_only_run", return_value=False),
            patch.object(streamlit_app, "_render_operation_actions") as render_actions,
            patch.object(streamlit_app, "_paint_activity"),
            patch.object(streamlit_app.st, "rerun") as rerun,
        ):
            finished = streamlit_app._poll_operation_and_actions(
                MagicMock(),
                MagicMock(),
                MagicMock(),
                MagicMock(),
                0,
                ["Camera"],
                settings,
                "Pixel",
            )
            if finished and streamlit_app._fragment_only_run():
                streamlit_app.st.rerun(scope="app")
        self.assertTrue(finished)
        render_actions.assert_called_once()
        rerun.assert_not_called()
        self.assertIsNone(session.operation_control)


class TestDeleteGateAfterVerify(unittest.TestCase):
    def test_delete_enabled_when_verify_ready_and_confirmed(self) -> None:
        settings = _sample_settings()
        fingerprint = settings_fingerprint(
            settings, "Pixel", ["Camera"]
        )
        session = SimpleNamespace(
            verify_status="ready",
            verify_fingerprint=fingerprint,
            verify_missing_count=0,
        )
        with patch.object(streamlit_app.st, "session_state", session):
            self.assertTrue(
                streamlit_app._delete_enabled(
                    settings, "Pixel", ["Camera"], delete_confirmed=True
                )
            )

    def test_delete_disabled_when_files_still_missing(self) -> None:
        settings = _sample_settings()
        fingerprint = settings_fingerprint(
            settings, "Pixel", ["Camera"]
        )
        session = SimpleNamespace(
            verify_status="blocked",
            verify_fingerprint=fingerprint,
            verify_missing_count=3,
        )
        with patch.object(streamlit_app.st, "session_state", session):
            reason = streamlit_app._delete_disabled_reason(
                settings, "Pixel", ["Camera"], delete_confirmed=True
            )
        self.assertIsNotNone(reason)
        self.assertIn("missing", reason.lower())


class TestFinalizeEmptyPreview(unittest.TestCase):
    def test_finalize_clears_stale_candidates_on_empty_preview(self) -> None:
        op = OperationControl(kind="preview")
        op.activity = {"mode": "idle", "label": "Preview complete"}
        op.transfer_candidates = []
        op.preview_fingerprint = "new-empty-preview"
        session = SimpleNamespace(
            activity={},
            import_stats=ImportStats(),
            delete_stats=DeleteStats(),
            transfer_candidates=["stale-candidate"],
            preview_fingerprint="old-preview",
            operation_control=op,
        )
        with patch.object(streamlit_app.st, "session_state", session):
            streamlit_app._finalize_operation(op)
        self.assertEqual(session.transfer_candidates, [])
        self.assertEqual(session.preview_fingerprint, "new-empty-preview")
        self.assertIsNone(session.operation_control)


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
        at = AppTest.from_file(_APP_FILE, default_timeout=30)
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
        at = AppTest.from_file(_APP_FILE, default_timeout=30)
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
        at = AppTest.from_file(_APP_FILE, default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        folder_select = next(w for w in at.multiselect if w.label == "DCIM folders")
        self.assertEqual(folder_select.value, ["Camera"])

    def test_delete_button_disabled_without_verify(
        self,
        _mock_list_devices,
        _mock_list_folders,
        _mock_open_device,
        _mock_close_device,
    ) -> None:
        at = AppTest.from_file(_APP_FILE, default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        delete_btn = next(b for b in at.button if b.label == "Delete from Phone")
        self.assertTrue(delete_btn.disabled)

    def test_verify_enabled_after_finished_operation_reclaimed(
        self,
        _mock_list_devices,
        _mock_list_folders,
        _mock_open_device,
        _mock_close_device,
    ) -> None:
        """A stopped import must not leave Verify Transfer stuck disabled."""
        at = AppTest.from_file(_APP_FILE, default_timeout=30)
        at.run()
        self.assertFalse(at.exception)

        op = OperationControl(kind="_import_worker")
        op.running = False
        op.finished = True
        op.activity = {
            "mode": "idle",
            "label": "Import stopped",
            "detail": "cancelled",
            "progress": 0.0,
            "progress_text": "",
            "show_progress": False,
            "outcome": "warning",
        }
        at.session_state.operation_control = op
        at.run()
        self.assertFalse(at.exception)

        verify_btn = next(b for b in at.button if b.label == "Verify Transfer")
        self.assertFalse(verify_btn.disabled)
        self.assertIsNone(at.session_state.operation_control)


if __name__ == "__main__":
    unittest.main()
