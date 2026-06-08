"""Import photos and videos from a USB MTP phone to a local drive."""

from __future__ import annotations

import threading
import tkinter as tk
from datetime import timedelta
from pathlib import Path
from tkinter import filedialog

import streamlit as st

from transfer.errors import is_disk_full_error
from transfer.events import TransferEvent, append_event
from transfer.importer import ImportStats, import_files
from transfer.mtp_client import (
    close_device,
    default_folder_selection,
    list_dcim_folders,
    list_devices,
    open_device,
    reset_device_cache,
)
from transfer.operation_control import OperationControl
from transfer.phone_cleanup import DeleteStats, delete_folders
from transfer.preview import TransferCandidate, list_transfer_candidates
from transfer.settings import DEFAULT_DEST, DEFAULT_TEMPLATE, TransferSettings, settings_fingerprint
from transfer.verify import verify_transfer

ACTIVITY_IDLE = "idle"
ACTIVITY_IMPORTING = "importing"
ACTIVITY_PREVIEWING = "previewing"
ACTIVITY_VERIFYING = "verifying"
ACTIVITY_DELETING = "deleting"


def _default_activity() -> dict[str, object]:
    return {
        "mode": ACTIVITY_IDLE,
        "label": "Ready",
        "detail": "Select an action to begin.",
        "progress": 0.0,
        "progress_text": "",
        "show_progress": False,
        "outcome": None,
    }


def _init_session_state() -> None:
    defaults = {
        "transfer_log": [],
        "verify_status": None,
        "verify_fingerprint": "",
        "verify_missing_count": 0,
        "import_stats": ImportStats(),
        "delete_stats": DeleteStats(),
        "device_options": [],
        "dcim_folders": [],
        "last_device_index": 0,
        "transfer_candidates": [],
        "preview_fingerprint": "",
        "activity": _default_activity(),
        "devices_scanned": False,
        "dcim_folders_error": None,
        "operation_control": None,
        "operation_error": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _set_activity(**kwargs: object) -> None:
    for key, value in kwargs.items():
        st.session_state.activity[key] = value


def _operation_running() -> bool:
    op = st.session_state.get("operation_control")
    return bool(op and op.running)


def _append_op_event(op: OperationControl, event: TransferEvent) -> None:
    op.append_log(event.format_line())


def _sync_operation_to_session(op: OperationControl) -> None:
    st.session_state.activity.update(op.activity)
    st.session_state.import_stats = op.import_stats
    st.session_state.delete_stats = op.delete_stats


def _finalize_operation(op: OperationControl) -> None:
    _sync_operation_to_session(op)
    if op.log_lines:
        st.session_state.transfer_log.extend(op.log_lines)
        if len(st.session_state.transfer_log) > 500:
            st.session_state.transfer_log = st.session_state.transfer_log[-500:]
    if op.verify_status is not None:
        st.session_state.verify_status = op.verify_status
        st.session_state.verify_fingerprint = op.verify_fingerprint
        st.session_state.verify_missing_count = op.verify_missing_count
    if op.transfer_candidates:
        st.session_state.transfer_candidates = op.transfer_candidates
    if op.preview_fingerprint:
        st.session_state.preview_fingerprint = op.preview_fingerprint
    if op.invalidate_verify:
        _invalidate_verify()
    if op.delete_confirm_reset:
        st.session_state.delete_confirm = False
    if op.error:
        st.session_state.operation_error = op.error
    st.session_state.operation_control = None


def _launch_operation(worker, *args) -> None:
    if _operation_running():
        return
    op = OperationControl(kind=worker.__name__)
    op.activity = _default_activity()
    st.session_state.operation_control = op
    st.session_state.operation_error = None
    threading.Thread(target=worker, args=(op, *args), daemon=True).start()


@st.fragment(run_every=timedelta(seconds=0.3))
def _operation_monitor(
    badge_slot,
    detail_slot,
    progress_slot,
    metrics_slot,
) -> None:
    op = st.session_state.get("operation_control")
    if op is None:
        return
    _sync_operation_to_session(op)
    _paint_activity(badge_slot, detail_slot, progress_slot, metrics_slot)
    if op.finished:
        _finalize_operation(op)


def _activity_badge_markdown(mode: str, outcome: str | None) -> str:
    if outcome == "error":
        return ":red-badge[:material/error: Error]"
    if outcome == "success":
        return ":green-badge[:material/check_circle: Complete]"
    if outcome == "warning":
        return ":orange-badge[:material/warning: Stopped]"
    if outcome == "info":
        return ":blue-badge[:material/info: Done]"
    badges = {
        ACTIVITY_IDLE: ":blue-badge[:material/hourglass_empty: Ready]",
        ACTIVITY_IMPORTING: ":orange-badge[:material/download: Importing]",
        ACTIVITY_PREVIEWING: ":blue-badge[:material/preview: Previewing]",
        ACTIVITY_VERIFYING: ":blue-badge[:material/verified: Verifying]",
        ACTIVITY_DELETING: ":orange-badge[:material/delete: Deleting]",
    }
    return badges.get(mode, ":blue-badge[Working]")


def _paint_activity(
    badge_slot,
    detail_slot,
    progress_slot,
    metrics_slot=None,
) -> None:
    activity = st.session_state.activity
    badge_slot.markdown(_activity_badge_markdown(activity["mode"], activity["outcome"]))
    detail_slot.markdown(f"**{activity['label']}** — {activity['detail']}")
    if activity["show_progress"]:
        progress_slot.progress(
            float(activity["progress"]),
            text=str(activity["progress_text"])[:120],
        )
    else:
        progress_slot.empty()

    if metrics_slot is None:
        return
    if activity["mode"] == ACTIVITY_IMPORTING:
        slot = metrics_slot.empty()
        with slot.container():
            _render_status_metrics(st.session_state.import_stats)
    elif activity["mode"] == ACTIVITY_DELETING:
        slot = metrics_slot.empty()
        with slot.container():
            _render_delete_metrics(st.session_state.delete_stats)
    else:
        metrics_slot.empty()


def _render_activity_header(
    badge_slot,
    detail_slot,
    progress_slot,
    metrics_slot,
) -> None:
    _paint_activity(badge_slot, detail_slot, progress_slot, metrics_slot)


def _browse_folder() -> str | None:
    root = tk.Tk()
    root.withdraw()
    root.wm_attributes("-topmost", 1)
    selected = filedialog.askdirectory()
    root.destroy()
    return selected or None


def _refresh_devices(*, force_reconnect: bool = False) -> None:
    try:
        if force_reconnect:
            reset_device_cache(close_sessions=True)
        st.session_state.device_options = list_devices()
        if not st.session_state.device_options:
            st.sidebar.warning(
                "No phone detected. Connect in File transfer mode, unlock the phone, "
                "then click **Refresh devices**."
            )
    except Exception as exc:
        st.session_state.device_options = []
        st.sidebar.error(f"Device scan failed: {exc}")


def _load_dcim_folders(device_index: int) -> list[str]:
    device = open_device(device_index)
    try:
        return list_dcim_folders(device)
    finally:
        close_device(device)


def _try_load_dcim_folders(device_index: int) -> None:
    try:
        st.session_state.dcim_folders = _load_dcim_folders(device_index)
        st.session_state.dcim_folders_error = None
    except OSError as exc:
        st.session_state.dcim_folders = []
        st.session_state.dcim_folders_error = str(exc)


def _invalidate_verify() -> None:
    st.session_state.verify_status = None
    st.session_state.verify_fingerprint = ""
    st.session_state.verify_missing_count = 0


def _render_source_settings() -> tuple[int, list[str], str, bool, bool, bool]:
    st.subheader("Source")
    if st.button("Refresh devices", key="refresh_devices"):
        _refresh_devices(force_reconnect=True)
        st.session_state.devices_scanned = True
        st.session_state.dcim_folders = []
        st.session_state.dcim_folders_error = None
        _invalidate_verify()

    devices = st.session_state.device_options
    if not devices:
        st.caption("Connect your phone in File transfer mode, then refresh.")
        device_index = 0
        device_name = ""
    else:
        labels = [f"{d.name} ({d.description})" for d in devices]
        picked = st.selectbox("Device", labels, key="device_select")
        device_index = labels.index(picked)
        device_name = devices[device_index].name
        if device_index != st.session_state.last_device_index:
            _try_load_dcim_folders(device_index)
            st.session_state.last_device_index = device_index
            st.session_state.folder_select = default_folder_selection(
                st.session_state.dcim_folders
            )
            _invalidate_verify()

    if not st.session_state.dcim_folders and devices and not st.session_state.dcim_folders_error:
        _try_load_dcim_folders(device_index)
        if "folder_select" not in st.session_state:
            st.session_state.folder_select = default_folder_selection(
                st.session_state.dcim_folders
            )

    if st.session_state.dcim_folders_error:
        st.error(
            "Could not read DCIM folders from the phone. Unlock the device, confirm "
            "File transfer mode is enabled, then click **Refresh devices**.\n\n"
            f"Details: {st.session_state.dcim_folders_error}"
        )

    available = st.session_state.dcim_folders
    if "folder_select" not in st.session_state:
        st.session_state.folder_select = default_folder_selection(available)
    folders = st.multiselect(
        "DCIM folders",
        available,
        key="folder_select",
    )

    st.subheader("Filters")
    skip_trashed = st.checkbox("Skip trashed files", value=True, key="skip_trashed")
    skip_thumbnails = st.checkbox("Skip thumbnails", value=True, key="skip_thumbnails")
    skip_screenshots = st.checkbox("Skip screenshots", value=True, key="skip_screenshots")

    return device_index, folders, device_name, skip_trashed, skip_thumbnails, skip_screenshots


def _render_destination_settings() -> Path:
    st.subheader("Destination")
    dest_value = st.text_input("Folder", value=DEFAULT_DEST, key="dest_input")
    if st.button("Browse", key="browse_dest"):
        picked = _browse_folder()
        if picked:
            st.session_state.dest_input = picked
            _invalidate_verify()
    st.caption("Subfolder: Month (YYYY-MM)")
    return Path(dest_value)


def _render_rename_settings() -> tuple[bool, str]:
    st.subheader("File Names")
    rename_enabled = st.checkbox("Rename files", value=True, key="rename_enabled")
    st.caption("Template: ＄F-＄Y-＄M-＄D_＄H.＄N.＄S")
    st.caption("Photo example: IMG_1234-2026-06-08_19.14.27.jpg")
    st.caption("Video example: VID_20230301_200226-2023-03-01_20.02.26.mp4")
    st.caption("Ext case: lower")
    return rename_enabled, DEFAULT_TEMPLATE


def _render_import_settings() -> bool:
    st.subheader("Import options")
    return st.checkbox("Skip existing files", value=True, key="skip_existing")


def _build_settings(
    dest_root: Path,
    rename_enabled: bool,
    rename_template: str,
    skip_existing: bool,
    skip_trashed: bool,
    skip_thumbnails: bool,
    skip_screenshots: bool,
) -> TransferSettings:
    return TransferSettings(
        dest_root=dest_root,
        rename_enabled=rename_enabled,
        rename_template=rename_template,
        skip_existing=skip_existing,
        skip_trashed=skip_trashed,
        skip_thumbnails=skip_thumbnails,
        skip_screenshots=skip_screenshots,
    )


def _import_event_detail(event: TransferEvent) -> str:
    if event.dest and event.action in {"COPY", "SKIP", "ERROR"}:
        if event.reason:
            return f"{event.source} → {event.dest} ({event.reason})"
        return f"{event.source} → {event.dest}"
    if event.reason:
        return f"{event.source} ({event.reason})"
    return event.source


def _import_activity_detail(event: TransferEvent) -> str:
    if event.action == "COPY":
        return f"Copying {Path(event.source).name} → {Path(event.dest).name}"
    if event.action == "SKIP":
        return f"Skipping {Path(event.source).name}"
    if event.action == "ERROR":
        return f"Failed on {Path(event.source).name}"
    if event.action == "RETRY":
        return f"Retrying {Path(event.source).name} — file in use on phone"
    if event.action == "QUEUE":
        return f"Queued {event.reason or '0'} file(s) for import"
    if event.action == "STOPPED":
        return "Import stopped — target drive is full"
    if event.action == "SUMMARY":
        return event.source
    if event.action == "PHASE":
        return event.source
    return _import_event_detail(event)


def _import_progress_text(
    event: TransferEvent,
    stats: ImportStats,
    *,
    total_files: int,
    folders_scanned: int,
    folder_count: int,
) -> tuple[float, str]:
    detail = _import_activity_detail(event)
    if total_files > 0 and event.action in {"COPY", "SKIP", "ERROR", "STOPPED", "RETRY"}:
        fraction = min(stats.scanned / total_files, 1.0)
        return fraction, f"{stats.scanned}/{total_files} — {detail}"
    if event.action == "QUEUE":
        return 0.0, detail
    if event.action == "PHASE" and folder_count > 0 and "Scanning " in event.source:
        fraction = min(folders_scanned / folder_count, 0.99)
        return fraction, detail
    if event.action == "SUMMARY":
        return 1.0, detail
    if event.action in {"PHASE", "ERROR"}:
        return 0.0, detail
    return 0.0, detail


def _verify_and_store(
    device: object,
    folders: list[str],
    settings: TransferSettings,
    device_name: str,
    op: OperationControl,
    *,
    log_events: bool = True,
) -> tuple[str, int, bool]:
    fingerprint = settings_fingerprint(settings, device_name, folders)
    missing_count = 0
    status: str | None = None
    cancelled = False
    for event in verify_transfer(
        device,
        folders,
        settings,
        fingerprint,
        should_stop=op.should_stop,
    ):
        if event.action == "_RESULT":
            status = event.source
            missing_count = int(event.reason or "0")
            continue
        if event.action == "STOPPED":
            cancelled = True
        if log_events:
            _append_op_event(op, event)
    op.verify_missing_count = missing_count
    op.verify_fingerprint = fingerprint
    if status is None:
        status = "blocked" if missing_count else "ready"
    op.verify_status = status
    return status, missing_count, cancelled


def _update_import_operation(
    op: OperationControl,
    event: TransferEvent,
    stats: ImportStats,
    *,
    total_files: int,
    folders_scanned: int,
    folder_count: int,
) -> None:
    fraction, progress_text = _import_progress_text(
        event,
        stats,
        total_files=total_files,
        folders_scanned=folders_scanned,
        folder_count=folder_count,
    )
    op.import_stats = stats
    op.set_activity(
        mode=ACTIVITY_IMPORTING,
        label="Importing files",
        detail=_import_activity_detail(event),
        progress=fraction,
        progress_text=progress_text,
        show_progress=True,
        outcome=None,
    )


def _import_worker(
    op: OperationControl,
    device_index: int,
    folders: list[str],
    settings: TransferSettings,
    device_name: str,
) -> None:
    _append_op_event(op, TransferEvent(action="PHASE", source="--- Import started ---"))
    op.import_stats = ImportStats()
    op.set_activity(
        mode=ACTIVITY_IMPORTING,
        label="Importing files",
        detail="Connecting to phone...",
        progress=0.0,
        progress_text="Starting import...",
        show_progress=True,
        outcome=None,
    )
    device = open_device(device_index)
    try:
        total_files = 0
        folders_scanned = 0
        folder_count = len(folders)
        last_event = TransferEvent(action="PHASE", source="Import complete")
        disk_full_stopped = False
        user_cancelled = False
        disk_full_message = ""

        for event, stats in import_files(
            device,
            folders,
            settings,
            should_stop=op.should_stop,
        ):
            last_event = event
            _append_op_event(op, event)

            if event.action == "STOPPED":
                if is_disk_full_error(event.reason):
                    disk_full_stopped = True
                    disk_full_message = event.reason or ""
                elif event.reason == "cancelled by user":
                    user_cancelled = True
            elif event.action == "ERROR" and is_disk_full_error(event.reason):
                disk_full_stopped = True
                disk_full_message = event.reason or ""

            if event.action == "QUEUE":
                total_files = int(event.reason or 0)
            elif (
                event.action == "PHASE"
                and event.source.startswith("DCIM/")
                and not event.source.startswith("Scanning")
            ):
                folders_scanned += 1

            _update_import_operation(
                op,
                event,
                stats,
                total_files=total_files,
                folders_scanned=folders_scanned,
                folder_count=folder_count,
            )

        if user_cancelled:
            op.set_activity(
                mode=ACTIVITY_IDLE,
                label="Import stopped",
                detail=_import_activity_detail(last_event),
                progress=min(stats.scanned / max(total_files, 1), 1.0),
                progress_text="Import stopped by user",
                show_progress=False,
                outcome="warning",
            )
        elif disk_full_stopped:
            op.invalidate_verify = True
            op.set_activity(
                mode=ACTIVITY_IMPORTING,
                label="Import stopped",
                detail="Target drive is full — free space and try again.",
                progress=min(stats.scanned / max(total_files, 1), 1.0),
                progress_text="Import stopped — target drive is full",
                show_progress=True,
                outcome="warning",
            )
            op.error = (
                "Import stopped because the destination drive is full. "
                "Free space on the target drive, then run **Preview transfer list** "
                "and **Start Import** again to copy remaining files.\n\n"
                f"Details: {disk_full_message}"
            )
        else:
            verify_status, missing_count, verify_cancelled = _verify_and_store(
                device,
                folders,
                settings,
                device_name,
                op,
            )
            if verify_cancelled:
                op.set_activity(
                    mode=ACTIVITY_IDLE,
                    label="Import stopped",
                    detail="Verify stopped by user after import.",
                    progress=1.0,
                    progress_text="Stopped",
                    show_progress=False,
                    outcome="warning",
                )
            elif verify_status == "ready":
                op.set_activity(
                    mode=ACTIVITY_IDLE,
                    label="Import complete",
                    detail=(
                        f"{_import_activity_detail(last_event)} "
                        "Verify passed — check the confirmation box next to Delete from Phone."
                    ),
                    progress=1.0,
                    progress_text="Import complete",
                    show_progress=False,
                    outcome="success",
                )
            else:
                op.set_activity(
                    mode=ACTIVITY_IDLE,
                    label="Import complete",
                    detail=(
                        f"{_import_activity_detail(last_event)} "
                        f"Verify found {missing_count} file(s) missing at destination."
                    ),
                    progress=1.0,
                    progress_text="Import complete",
                    show_progress=False,
                    outcome="warning",
                )
    except OSError as exc:
        _append_op_event(op, TransferEvent(action="ERROR", source="Import", reason=str(exc)))
        op.set_activity(
            mode=ACTIVITY_IDLE,
            label="Import failed",
            detail=str(exc),
            progress=0.0,
            progress_text="",
            show_progress=False,
            outcome="error",
        )
        op.error = (
            "Import failed while reading from the phone. "
            "Unlock the device, confirm File transfer mode is enabled, click "
            "**Refresh devices** in the sidebar, then try again.\n\n"
            f"Details: {exc}"
        )
    except Exception as exc:
        _append_op_event(op, TransferEvent(action="ERROR", source="Import", reason=str(exc)))
        op.set_activity(
            mode=ACTIVITY_IDLE,
            label="Import failed",
            detail=str(exc),
            progress=0.0,
            progress_text="",
            show_progress=False,
            outcome="error",
        )
        op.error = f"Import failed: {exc}"
    finally:
        close_device(device)
        op.running = False
        op.finished = True


def _verify_worker(
    op: OperationControl,
    device_index: int,
    folders: list[str],
    settings: TransferSettings,
    device_name: str,
) -> None:
    fingerprint = settings_fingerprint(settings, device_name, folders)
    op.set_activity(
        mode=ACTIVITY_VERIFYING,
        label="Verifying transfer",
        detail="Comparing phone files with destination...",
        progress=0.0,
        progress_text="Verifying...",
        show_progress=True,
        outcome=None,
    )
    device = open_device(device_index)
    checked = 0
    cancelled = False
    try:
        missing_count = 0
        status: str | None = None
        for event in verify_transfer(
            device,
            folders,
            settings,
            fingerprint,
            should_stop=op.should_stop,
        ):
            if event.action == "_RESULT":
                status = event.source
                missing_count = int(event.reason or "0")
                continue
            if event.action == "STOPPED":
                cancelled = True
            _append_op_event(op, event)
            if event.action in {"VERIFY", "FAIL", "READY", "BLOCKED", "ERROR", "WARN"}:
                checked += 1
                op.set_activity(
                    mode=ACTIVITY_VERIFYING,
                    label="Verifying transfer",
                    detail=event.source,
                    progress=min(checked / max(checked + 5, 1), 0.95),
                    progress_text=f"Verifying — {event.source[:80]}",
                    show_progress=True,
                    outcome=None,
                )
        op.verify_missing_count = missing_count
        op.verify_fingerprint = fingerprint
        op.verify_status = status or ("blocked" if missing_count else "ready")
        if cancelled:
            op.set_activity(
                mode=ACTIVITY_IDLE,
                label="Verify stopped",
                detail="Verification stopped by user.",
                progress=min(checked / max(checked + 1, 1), 1.0),
                progress_text="Verify stopped",
                show_progress=False,
                outcome="warning",
            )
        elif op.verify_status == "ready":
            op.set_activity(
                mode=ACTIVITY_IDLE,
                label="Verify complete",
                detail="All importable files found at destination.",
                progress=1.0,
                progress_text="Verify complete",
                show_progress=False,
                outcome="success",
            )
        else:
            op.set_activity(
                mode=ACTIVITY_IDLE,
                label="Verify complete",
                detail=f"{missing_count} file(s) missing or mismatched.",
                progress=1.0,
                progress_text="Verify complete",
                show_progress=False,
                outcome="warning",
            )
    except Exception as exc:
        op.verify_status = "blocked"
        _append_op_event(op, TransferEvent(action="ERROR", source="Verify", reason=str(exc)))
        op.set_activity(
            mode=ACTIVITY_IDLE,
            label="Verify failed",
            detail=str(exc),
            progress=0.0,
            progress_text="",
            show_progress=False,
            outcome="error",
        )
        op.error = f"Verify failed: {exc}"
    finally:
        close_device(device)
        op.running = False
        op.finished = True


def _delete_worker(
    op: OperationControl,
    device_index: int,
    folders: list[str],
    settings: TransferSettings,
) -> None:
    op.set_activity(
        mode=ACTIVITY_DELETING,
        label="Deleting from phone",
        detail="Starting phone cleanup...",
        progress=0.0,
        progress_text="Delete starting...",
        show_progress=True,
        outcome=None,
    )
    device = open_device(device_index)
    total_files = 0
    processed = 0
    cancelled = False
    try:
        op.set_activity(
            mode=ACTIVITY_DELETING,
            label="Deleting from phone",
            detail="Counting files on phone...",
            progress=0.0,
            progress_text="Scanning phone folders...",
            show_progress=True,
            outcome=None,
        )
        for event, stats in delete_folders(
            device,
            folders,
            skip_trashed=settings.skip_trashed,
            skip_thumbnails=settings.skip_thumbnails,
            skip_screenshots=settings.skip_screenshots,
            should_stop=op.should_stop,
        ):
            _append_op_event(op, event)
            op.delete_stats = stats
            if event.action == "STOPPED":
                cancelled = True
                break
            if event.action == "QUEUE":
                total_files = int(event.reason or stats.total_files or 0)
                op.set_activity(
                    mode=ACTIVITY_DELETING,
                    label="Deleting from phone",
                    detail=f"{total_files} file(s) to delete",
                    progress=0.0,
                    progress_text=f"0/{total_files} — Starting delete...",
                    show_progress=True,
                    outcome=None,
                )
                continue
            if event.action in {"DELETE", "ERROR"}:
                processed += 1
                fraction = min(processed / max(total_files, 1), 1.0)
                op.set_activity(
                    mode=ACTIVITY_DELETING,
                    label="Deleting from phone",
                    detail=f"Deleting {Path(event.source).name}",
                    progress=fraction,
                    progress_text=(
                        f"{processed}/{total_files} — "
                        f"Deleting {Path(event.source).name}"
                    ),
                    show_progress=True,
                    outcome=None,
                )
        if cancelled:
            op.set_activity(
                mode=ACTIVITY_IDLE,
                label="Delete stopped",
                detail=(
                    f"Stopped after {op.delete_stats.files_deleted} file(s) and "
                    f"{op.delete_stats.folders_deleted} folder(s)."
                ),
                progress=min(processed / max(total_files, 1), 1.0),
                progress_text="Delete stopped by user",
                show_progress=False,
                outcome="warning",
            )
        else:
            op.set_activity(
                mode=ACTIVITY_IDLE,
                label="Delete complete",
                detail=(
                    f"Removed {op.delete_stats.folders_deleted} folder(s) and "
                    f"{op.delete_stats.files_deleted} file(s) from the phone."
                ),
                progress=1.0,
                progress_text="Delete complete",
                show_progress=False,
                outcome="success",
            )
            op.invalidate_verify = True
            op.delete_confirm_reset = True
    except Exception as exc:
        _append_op_event(op, TransferEvent(action="ERROR", source="Delete", reason=str(exc)))
        op.set_activity(
            mode=ACTIVITY_IDLE,
            label="Delete failed",
            detail=str(exc),
            progress=0.0,
            progress_text="",
            show_progress=False,
            outcome="error",
        )
        op.error = f"Delete failed: {exc}"
    finally:
        close_device(device)
        op.running = False
        op.finished = True


def _delete_enabled(
    settings: TransferSettings,
    device_name: str,
    folders: list[str],
    delete_confirmed: bool,
) -> bool:
    return _delete_disabled_reason(settings, device_name, folders, delete_confirmed) is None


def _delete_disabled_reason(
    settings: TransferSettings,
    device_name: str,
    folders: list[str],
    delete_confirmed: bool,
) -> str | None:
    if not folders:
        return "Select at least one DCIM folder in the sidebar."
    if not delete_confirmed:
        return (
            "Check **I confirm these folders are backed up and should be deleted "
            "from the phone** next to the Delete button."
        )
    if st.session_state.verify_status is None:
        return "Run **Verify Transfer** first (or complete **Start Import** to verify automatically)."
    if st.session_state.verify_status != "ready":
        return (
            f"Verify found {st.session_state.verify_missing_count} file(s) missing at "
            "destination — import or fix those files, then verify again."
        )
    current = settings_fingerprint(settings, device_name, folders)
    if current != st.session_state.verify_fingerprint:
        return "Settings changed since the last verify — run **Verify Transfer** again."
    return None


def _render_delete_metrics(stats: DeleteStats | None = None) -> None:
    stats = stats if stats is not None else st.session_state.delete_stats
    cols = st.columns(4)
    cols[0].metric("Total files", stats.total_files)
    cols[1].metric("Files deleted", stats.files_deleted)
    cols[2].metric("Folders deleted", stats.folders_deleted)
    cols[3].metric("Errors", stats.errors)


def _render_status_metrics(stats: ImportStats | None = None) -> None:
    stats = stats if stats is not None else st.session_state.import_stats
    cols = st.columns(6)
    cols[0].metric("Scanned", stats.scanned)
    cols[1].metric("Copied", stats.copied)
    cols[2].metric("Skip existing", stats.skipped_existing)
    cols[3].metric("Skip filter", stats.skipped_filter)
    cols[4].metric("Errors", stats.errors)
    status = st.session_state.verify_status
    if status == "ready":
        cols[5].metric("Verify", "Ready")
    elif status == "blocked":
        cols[5].metric("Verify", f"Blocked ({st.session_state.verify_missing_count})")
    else:
        cols[5].metric("Verify", "Not run")


def _preview_worker(
    op: OperationControl,
    device_index: int,
    folders: list[str],
    settings: TransferSettings,
    device_name: str,
) -> None:
    op.set_activity(
        mode=ACTIVITY_PREVIEWING,
        label="Previewing transfer list",
        detail="Scanning phone folders...",
        progress=0.0,
        progress_text="Scanning phone...",
        show_progress=True,
        outcome=None,
    )
    device = open_device(device_index)
    try:
        op.set_activity(
            mode=ACTIVITY_PREVIEWING,
            label="Previewing transfer list",
            detail="Reading files from phone...",
            progress=0.35,
            progress_text="Reading files from phone...",
            show_progress=True,
            outcome=None,
        )
        candidates = list_transfer_candidates(
            device,
            folders,
            settings,
            should_stop=op.should_stop,
        )
        if candidates is None:
            op.set_activity(
                mode=ACTIVITY_IDLE,
                label="Preview stopped",
                detail="Preview stopped by user.",
                progress=0.0,
                progress_text="Preview stopped",
                show_progress=False,
                outcome="warning",
            )
            return
        op.transfer_candidates = candidates
        op.preview_fingerprint = settings_fingerprint(settings, device_name, folders)
        if candidates:
            new_count = sum(1 for candidate in candidates if not candidate.already_exists)
            detail = (
                f"{len(candidates)} importable file(s) on phone, "
                f"{new_count} would be copied."
            )
            outcome = "success"
        else:
            detail = "No importable files match the current filters."
            outcome = "info"
        op.set_activity(
            mode=ACTIVITY_IDLE,
            label="Preview complete",
            detail=detail,
            progress=1.0,
            progress_text="Preview complete",
            show_progress=False,
            outcome=outcome,
        )
    except Exception as exc:
        _append_op_event(op, TransferEvent(action="ERROR", source="Preview", reason=str(exc)))
        op.set_activity(
            mode=ACTIVITY_IDLE,
            label="Preview failed",
            detail=str(exc),
            progress=0.0,
            progress_text="",
            show_progress=False,
            outcome="error",
        )
        op.error = f"Preview failed: {exc}"
    finally:
        close_device(device)
        op.running = False
        op.finished = True


def _require_device_and_folders(folders: list[str]) -> bool:
    if not st.session_state.device_options:
        st.warning("No phone detected. Connect the device and refresh.")
        return False
    if not folders:
        st.warning("Select at least one DCIM folder.")
        return False
    return True


def _request_stop() -> None:
    op = st.session_state.get("operation_control")
    if op and op.running:
        op.request_stop()


def _stop_disabled_for(worker_name: str) -> bool:
    op = st.session_state.get("operation_control")
    return not (op and op.running and op.kind == worker_name)


def _render_transfer_candidates(current_fingerprint: str) -> None:
    if not st.session_state.preview_fingerprint:
        return
    if st.session_state.preview_fingerprint != current_fingerprint:
        st.warning("Settings changed since the last preview. Click **Preview transfer list** to refresh.")
        return

    candidates: list[TransferCandidate] = st.session_state.transfer_candidates
    st.subheader("Transfer preview")
    st.caption(
        "Planned renames for importable phone files. "
        "Photos may use EXIF capture time after import; videos use the timestamp embedded in the filename."
    )
    if not candidates:
        st.info("No importable files match the current filters.")
        return

    st.dataframe(
        [
            {
                "Source on phone": c.source_path,
                "Original name": c.original_name,
                "New name": c.new_name,
                "Rename": f"{c.original_name} → {c.new_name}",
                "Already exists": "Yes" if c.already_exists else "No",
                "Destination path": c.dest_path,
            }
            for c in candidates
        ],
        column_order=[
            "Source on phone",
            "Original name",
            "New name",
            "Rename",
            "Already exists",
            "Destination path",
        ],
        width="stretch",
        hide_index=False,
    )


def _render_verbose_log() -> None:
    st.subheader("Activity log")
    if st.button("Clear log", key="clear_log"):
        st.session_state.transfer_log = []
    log_text = "\n".join(st.session_state.transfer_log)
    st.code(log_text or "(no log entries yet)", language=None)


def main() -> None:
    st.set_page_config(page_title="Import Photos and Videos", layout="wide")
    _init_session_state()
    if not st.session_state.devices_scanned:
        _refresh_devices()
        st.session_state.devices_scanned = True

    with st.sidebar:
        device_index, folders, device_name, skip_trashed, skip_thumbnails, skip_screenshots = (
            _render_source_settings()
        )
        dest_root = _render_destination_settings()
        rename_enabled, rename_template = _render_rename_settings()
        skip_existing = _render_import_settings()

    settings = _build_settings(
        dest_root,
        rename_enabled,
        rename_template,
        skip_existing,
        skip_trashed,
        skip_thumbnails,
        skip_screenshots,
    )
    current_fingerprint = settings_fingerprint(settings, device_name, folders)
    if (
        st.session_state.verify_fingerprint
        and current_fingerprint != st.session_state.verify_fingerprint
    ):
        _invalidate_verify()

    st.title("Import Photos and Videos")

    with st.container(border=True):
        st.markdown("##### :material/info: Current activity")
        activity_badge = st.empty()
        activity_detail = st.empty()
        activity_progress = st.empty()
        activity_metrics = st.empty()
        _render_activity_header(
            activity_badge,
            activity_detail,
            activity_progress,
            activity_metrics,
        )

    running = _operation_running()
    with st.container(horizontal=True):
        if st.button("Start Import", type="primary", key="start_import", disabled=running):
            st.session_state.operation_error = None
            if _require_device_and_folders(folders):
                _launch_operation(
                    _import_worker,
                    device_index,
                    folders,
                    settings,
                    device_name,
                )
        if st.button(
            "Stop Import",
            key="stop_import",
            disabled=_stop_disabled_for("_import_worker"),
        ):
            _request_stop()
        if st.button("Preview transfer list", key="preview_transfer", disabled=running):
            st.session_state.operation_error = None
            if _require_device_and_folders(folders):
                _launch_operation(
                    _preview_worker,
                    device_index,
                    folders,
                    settings,
                    device_name,
                )
        if st.button(
            "Stop Preview",
            key="stop_preview",
            disabled=_stop_disabled_for("_preview_worker"),
        ):
            _request_stop()
        if st.button("Verify Transfer", key="verify_transfer", disabled=running):
            st.session_state.operation_error = None
            if _require_device_and_folders(folders):
                _launch_operation(
                    _verify_worker,
                    device_index,
                    folders,
                    settings,
                    device_name,
                )
        if st.button(
            "Stop Verify",
            key="stop_verify",
            disabled=_stop_disabled_for("_verify_worker"),
        ):
            _request_stop()
        delete_confirmed = st.checkbox(
            "I confirm these folders are backed up and should be deleted from the phone",
            value=False,
            key="delete_confirm",
            disabled=running,
        )
        delete_ok = _delete_enabled(settings, device_name, folders, delete_confirmed)
        if st.button(
            "Delete from Phone",
            key="delete_phone",
            disabled=not delete_ok or running,
        ):
            st.session_state.operation_error = None
            if _require_device_and_folders(folders):
                _launch_operation(
                    _delete_worker,
                    device_index,
                    folders,
                    settings,
                )
        if st.button(
            "Stop Delete",
            key="stop_delete",
            disabled=_stop_disabled_for("_delete_worker"),
        ):
            _request_stop()
    delete_reason = _delete_disabled_reason(
        settings, device_name, folders, delete_confirmed
    )
    if delete_reason and not running:
        st.caption(f"Delete from Phone is disabled: {delete_reason}")

    _operation_monitor(
        activity_badge,
        activity_detail,
        activity_progress,
        activity_metrics,
    )
    if st.session_state.get("operation_error"):
        st.error(st.session_state.operation_error)

    _render_transfer_candidates(current_fingerprint)
    _render_status_metrics()
    _render_verbose_log()


if __name__ == "__main__":
    main()
