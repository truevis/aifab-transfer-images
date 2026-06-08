"""Import photos and videos from a USB MTP phone to a local drive."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog

import streamlit as st

from transfer.events import TransferEvent, append_event
from transfer.importer import ImportStats, import_files
from transfer.mtp_client import (
    close_device,
    default_folder_selection,
    list_dcim_folders,
    list_devices,
    open_device,
    refresh_open_device,
    reset_device_cache,
)
from transfer.phone_cleanup import DeleteStats, delete_folders
from transfer.preview import TransferCandidate, list_transfer_candidates
from transfer.settings import DEFAULT_DEST, DEFAULT_TEMPLATE, TransferSettings, settings_fingerprint
from transfer.verify import verify_transfer


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
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _browse_folder() -> str | None:
    root = tk.Tk()
    root.withdraw()
    root.wm_attributes("-topmost", 1)
    selected = filedialog.askdirectory()
    root.destroy()
    return selected or None


def _refresh_devices() -> None:
    try:
        reset_device_cache()
        st.session_state.device_options = list_devices()
    except Exception as exc:
        st.session_state.device_options = []
        st.sidebar.error(f"Device scan failed: {exc}")


def _load_dcim_folders(device_index: int) -> list[str]:
    device = open_device(device_index)
    try:
        return list_dcim_folders(device)
    finally:
        close_device(device)


def _invalidate_verify() -> None:
    st.session_state.verify_status = None
    st.session_state.verify_fingerprint = ""
    st.session_state.verify_missing_count = 0


def _render_source_settings() -> tuple[int, list[str], str, bool, bool, bool]:
    st.subheader("Source")
    if st.button("Refresh devices", key="refresh_devices"):
        _refresh_devices()
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
            st.session_state.dcim_folders = _load_dcim_folders(device_index)
            st.session_state.last_device_index = device_index
            st.session_state.folder_select = default_folder_selection(
                st.session_state.dcim_folders
            )
            _invalidate_verify()

    if not st.session_state.dcim_folders and devices:
        st.session_state.dcim_folders = _load_dcim_folders(device_index)
        if "folder_select" not in st.session_state:
            st.session_state.folder_select = default_folder_selection(
                st.session_state.dcim_folders
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


def _render_cleanup_settings() -> bool:
    st.subheader("Phone cleanup")
    return st.checkbox(
        "I confirm these folders are backed up and should be deleted from the phone",
        value=False,
        key="delete_confirm",
    )


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
    if event.action == "COPY" and event.dest:
        return f"{event.source} → {event.dest}"
    if event.reason:
        return f"{event.source} ({event.reason})"
    return event.source


def _import_progress_text(
    event: TransferEvent,
    stats: ImportStats,
    *,
    total_files: int,
    folders_scanned: int,
    folder_count: int,
) -> tuple[float, str]:
    detail = _import_event_detail(event)
    if total_files > 0 and event.action in {"COPY", "SKIP", "ERROR"}:
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


def _run_import(device_index: int, folders: list[str], settings: TransferSettings) -> None:
    if not st.session_state.device_options:
        st.warning("No phone detected. Connect the device and refresh.")
        return
    if not folders:
        st.warning("Select at least one DCIM folder.")
        return

    append_event(
        st.session_state.transfer_log,
        TransferEvent(action="PHASE", source="--- Import started ---"),
        echo_terminal=False,
    )
    st.session_state.import_stats = ImportStats()

    device = refresh_open_device(device_index)
    try:
        st.subheader("Import progress")
        progress = st.progress(0.0, text="Starting import...")
        status_line = st.empty()
        metrics_slot = st.empty()
        log_slot = st.empty()

        total_files = 0
        folders_scanned = 0
        folder_count = len(folders)
        last_event = TransferEvent(action="PHASE", source="Import complete")

        for event, stats in import_files(device, folders, settings):
            last_event = event
            append_event(st.session_state.transfer_log, event, echo_terminal=False)
            st.session_state.import_stats = stats

            if event.action == "QUEUE":
                total_files = int(event.reason or 0)
            elif (
                event.action == "PHASE"
                and event.source.startswith("DCIM/")
                and not event.source.startswith("Scanning")
            ):
                folders_scanned += 1

            fraction, progress_text = _import_progress_text(
                event,
                stats,
                total_files=total_files,
                folders_scanned=folders_scanned,
                folder_count=folder_count,
            )
            progress.progress(fraction, text=progress_text[:120])
            status_line.markdown(f"**{event.action}** — {_import_event_detail(event)}")

            with metrics_slot.container():
                _render_status_metrics(stats)

            log_tail = st.session_state.transfer_log[-80:]
            log_slot.code("\n".join(log_tail), language=None)

        progress.progress(1.0, text="Import complete")
        status_line.success(_import_event_detail(last_event))
        _invalidate_verify()
    except OSError as exc:
        append_event(
            st.session_state.transfer_log,
            TransferEvent(action="ERROR", source="Import", reason=str(exc)),
        )
        st.error(
            "Import failed while reading from the phone. "
            "Unlock the device, confirm File transfer mode is enabled, click "
            "**Refresh devices** in the sidebar, then try again.\n\n"
            f"Details: {exc}"
        )
    except Exception as exc:
        append_event(
            st.session_state.transfer_log,
            TransferEvent(action="ERROR", source="Import", reason=str(exc)),
        )
        st.error(f"Import failed: {exc}")
    finally:
        close_device(device)


def _run_verify(
    device_index: int,
    folders: list[str],
    settings: TransferSettings,
    device_name: str,
) -> None:
    if not st.session_state.device_options:
        st.warning("No phone detected. Connect the device and refresh.")
        return
    if not folders:
        st.warning("Select at least one DCIM folder.")
        return
    fingerprint = settings_fingerprint(settings, device_name, folders)
    device = refresh_open_device(device_index)
    missing_count = 0
    try:
        for event in verify_transfer(device, folders, settings, fingerprint):
            if event.action == "_RESULT":
                st.session_state.verify_status = event.source
                missing_count = int(event.reason or "0")
                continue
            append_event(st.session_state.transfer_log, event)
        st.session_state.verify_missing_count = missing_count
        st.session_state.verify_fingerprint = fingerprint
        if st.session_state.verify_status is None:
            st.session_state.verify_status = "blocked" if missing_count else "ready"
    except Exception as exc:
        st.session_state.verify_status = "blocked"
        append_event(
            st.session_state.transfer_log,
            TransferEvent(action="ERROR", source="Verify", reason=str(exc)),
        )
    finally:
        close_device(device)


def _run_delete(
    device_index: int,
    folders: list[str],
    settings: TransferSettings,
) -> None:
    if not st.session_state.device_options:
        st.warning("No phone detected. Connect the device and refresh.")
        return
    device = open_device(device_index)
    try:
        progress = st.progress(0.0, text="Delete starting...")
        step = 0
        for event, stats in delete_folders(
            device,
            folders,
            skip_trashed=settings.skip_trashed,
            skip_thumbnails=settings.skip_thumbnails,
            skip_screenshots=settings.skip_screenshots,
        ):
            append_event(st.session_state.transfer_log, event)
            st.session_state.delete_stats = stats
            if event.action in {"DELETE", "ERROR"}:
                step += 1
                progress.progress(min(step / max(step + 1, 2), 1.0), text=event.source[:80])
        progress.progress(1.0, text="Delete complete")
        _invalidate_verify()
        st.session_state.delete_confirm = False
    except Exception as exc:
        append_event(
            st.session_state.transfer_log,
            TransferEvent(action="ERROR", source="Delete", reason=str(exc)),
        )
    finally:
        close_device(device)


def _delete_enabled(
    settings: TransferSettings,
    device_name: str,
    folders: list[str],
    delete_confirmed: bool,
) -> bool:
    if not delete_confirmed:
        return False
    if st.session_state.verify_status != "ready":
        return False
    current = settings_fingerprint(settings, device_name, folders)
    return current == st.session_state.verify_fingerprint


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


def _run_preview(
    device_index: int,
    folders: list[str],
    settings: TransferSettings,
    device_name: str,
) -> None:
    if not st.session_state.device_options:
        st.warning("No phone detected. Connect the device and refresh.")
        return
    if not folders:
        st.warning("Select at least one DCIM folder.")
        return
    device = refresh_open_device(device_index)
    try:
        with st.spinner("Scanning phone for transfer candidates..."):
            st.session_state.transfer_candidates = list_transfer_candidates(
                device, folders, settings
            )
            st.session_state.preview_fingerprint = settings_fingerprint(
                settings, device_name, folders
            )
    except Exception as exc:
        st.session_state.transfer_candidates = []
        append_event(
            st.session_state.transfer_log,
            TransferEvent(action="ERROR", source="Preview", reason=str(exc)),
        )
    finally:
        close_device(device)


def _render_transfer_candidates(current_fingerprint: str) -> None:
    if st.session_state.preview_fingerprint != current_fingerprint:
        return
    candidates: list[TransferCandidate] = st.session_state.transfer_candidates
    if not candidates:
        return
    st.subheader(f"Transfer candidates ({len(candidates)})")
    st.caption(
        "Files that would be copied with the current filters and skip-existing settings. "
        "New names use the phone file date; EXIF capture time may differ slightly after import."
    )
    st.dataframe(
        [
            {
                "Source path": c.source_path,
                "Original name": c.original_name,
                "New name": c.new_name,
                "Destination": c.dest_path,
            }
            for c in candidates
        ],
        width="stretch",
        hide_index=True,
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
    if not st.session_state.device_options:
        _refresh_devices()

    with st.sidebar:
        device_index, folders, device_name, skip_trashed, skip_thumbnails, skip_screenshots = (
            _render_source_settings()
        )
        dest_root = _render_destination_settings()
        rename_enabled, rename_template = _render_rename_settings()
        skip_existing = _render_import_settings()
        delete_confirmed = _render_cleanup_settings()

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

    with st.container(horizontal=True):
        if st.button("Start Import", type="primary", key="start_import"):
            _run_import(device_index, folders, settings)
        if st.button("Preview transfer list", key="preview_transfer"):
            _run_preview(device_index, folders, settings, device_name)
        if st.button("Verify Transfer", key="verify_transfer"):
            _run_verify(device_index, folders, settings, device_name)
        delete_ok = _delete_enabled(settings, device_name, folders, delete_confirmed)
        if st.button("Delete from Phone", key="delete_phone", disabled=not delete_ok):
            _run_delete(device_index, folders, settings)

    _render_transfer_candidates(current_fingerprint)
    _render_status_metrics()
    _render_verbose_log()


if __name__ == "__main__":
    main()
