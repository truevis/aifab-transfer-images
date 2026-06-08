"""Import photos and videos from a USB MTP phone to a local drive (CLI)."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

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
from transfer.phone_cleanup import DeleteStats, delete_folders
from transfer.settings import DEFAULT_DEST, DEFAULT_TEMPLATE, TransferSettings, settings_fingerprint
from transfer.verify import verify_transfer


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transfer photos and videos from a USB MTP phone to a local drive.",
    )
    parser.add_argument(
        "--refresh-devices",
        action="store_true",
        help="Clear cached MTP device handles before scanning.",
    )
    parser.add_argument(
        "--device",
        type=int,
        default=0,
        metavar="INDEX",
        help="Device index from list-devices (default: 0).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_devices_parser = subparsers.add_parser(
        "list-devices",
        help="List connected MTP phones.",
    )
    list_devices_parser.set_defaults(func=_cmd_list_devices)

    list_folders_parser = subparsers.add_parser(
        "list-folders",
        help="List DCIM folders on the selected device.",
    )
    list_folders_parser.set_defaults(func=_cmd_list_folders)

    for name, handler in (
        ("import", _cmd_import),
        ("verify", _cmd_verify),
        ("delete", _cmd_delete),
    ):
        sub = subparsers.add_parser(name, help=handler.__doc__)
        _add_transfer_options(sub)
        sub.set_defaults(func=handler)

    return parser.parse_args(argv)


def _add_transfer_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path(DEFAULT_DEST),
        help=f"Destination root folder (default: {DEFAULT_DEST}).",
    )
    parser.add_argument(
        "--folders",
        nargs="+",
        metavar="NAME",
        help="DCIM folder names (default: Camera, OpenCamera, Expert RAW when present).",
    )
    parser.add_argument(
        "--rename",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Rename files using the template (default: enabled).",
    )
    parser.add_argument(
        "--template",
        default=DEFAULT_TEMPLATE,
        help=f"Rename template (default: {DEFAULT_TEMPLATE}).",
    )
    parser.add_argument(
        "--ext-lower",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use lowercase file extensions (default: enabled).",
    )
    parser.add_argument(
        "--skip-existing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip files already present at destination (default: enabled).",
    )
    parser.add_argument(
        "--skip-trashed",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip trashed files (default: enabled).",
    )
    parser.add_argument(
        "--skip-thumbnails",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip thumbnail files (default: enabled).",
    )
    parser.add_argument(
        "--skip-screenshots",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip screenshot files (default: enabled).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Process at most N importable files (useful for testing).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only print summary lines, not every file event.",
    )

    if parser.prog.endswith("delete"):
        parser.add_argument(
            "--confirm-backup",
            action="store_true",
            help="Confirm folders are backed up and should be deleted from the phone.",
        )
        parser.add_argument(
            "--force-delete",
            action="store_true",
            help="Delete even if verify would block (dangerous).",
        )


def _build_settings(args: argparse.Namespace) -> TransferSettings:
    return TransferSettings(
        dest_root=args.dest,
        rename_enabled=args.rename,
        rename_template=args.template,
        ext_lower=args.ext_lower,
        skip_existing=args.skip_existing,
        skip_trashed=args.skip_trashed,
        skip_thumbnails=args.skip_thumbnails,
        skip_screenshots=args.skip_screenshots,
    )


def _open_selected_device(args: argparse.Namespace):
    if args.refresh_devices:
        reset_device_cache()
    devices = list_devices()
    if not devices:
        print("No MTP phone detected. Connect the device in File transfer mode.", file=sys.stderr)
        sys.exit(1)
    if args.device < 0 or args.device >= len(devices):
        print(
            f"Device index {args.device} is out of range (0–{len(devices) - 1}).",
            file=sys.stderr,
        )
        sys.exit(1)
    return devices, open_device(args.device)


def _resolve_folders(device, args: argparse.Namespace) -> list[str]:
    if args.folders:
        return args.folders
    available = list_dcim_folders(device)
    selected = default_folder_selection(available)
    if not selected:
        print("No DCIM folders found on device.", file=sys.stderr)
        sys.exit(1)
    return selected


def _echo_event(log: list[str], event: TransferEvent, *, quiet: bool) -> None:
    if quiet and event.action not in {"PHASE", "SUMMARY", "ERROR", "READY", "BLOCKED", "VERIFY"}:
        return
    append_event(log, event, echo_terminal=True)


def _counts_toward_limit(event: TransferEvent) -> bool:
    if event.action in {"COPY", "ERROR"}:
        return True
    if event.action == "SKIP" and event.reason == "already exists at destination":
        return True
    return False


def _cmd_list_devices(args: argparse.Namespace) -> int:
    if args.refresh_devices:
        reset_device_cache()
    devices = list_devices()
    if not devices:
        print("No MTP phone detected.")
        return 1
    for device in devices:
        print(f"[{device.index}] {device.name} — {device.description}")
    return 0


def _cmd_list_folders(args: argparse.Namespace) -> int:
    devices, device = _open_selected_device(args)
    try:
        folders = list_dcim_folders(device)
        if not folders:
            print("No DCIM folders found.")
            return 1
        for name in folders:
            marker = " *" if name in default_folder_selection(folders) else ""
            print(f"{name}{marker}")
        print(f"\nDevice: {devices[args.device].name}")
        print("* = included in default folder selection")
    finally:
        close_device(device)
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    devices, device = _open_selected_device(args)
    log: list[str] = []
    stats = ImportStats()
    try:
        folders = _resolve_folders(device, args)
        settings = _build_settings(args)
        print(f"Device: {devices[args.device].name}")
        print(f"Folders: {', '.join(folders)}")
        print(f"Destination: {settings.dest_root}")
        if args.limit:
            print(f"Limit: {args.limit} importable file(s)")

        processed = 0
        for event, stats in import_files(device, folders, settings):
            _echo_event(log, event, quiet=args.quiet)
            if args.limit and _counts_toward_limit(event):
                processed += 1
                if processed >= args.limit:
                    append_event(
                        log,
                        TransferEvent(
                            action="PHASE",
                            source=f"Stopped after {args.limit} importable file(s) (--limit)",
                        ),
                    )
                    break
    except Exception as exc:
        append_event(log, TransferEvent(action="ERROR", source="Import", reason=str(exc)))
        return 1
    finally:
        close_device(device)

    print(
        f"\nDone — scanned {stats.scanned}, copied {stats.copied}, "
        f"skipped existing {stats.skipped_existing}, skipped filter {stats.skipped_filter}, "
        f"errors {stats.errors}"
    )
    return 0 if stats.errors == 0 else 1


def _cmd_verify(args: argparse.Namespace) -> int:
    devices, device = _open_selected_device(args)
    log: list[str] = []
    status = "blocked"
    missing_count = 0
    try:
        folders = _resolve_folders(device, args)
        settings = _build_settings(args)
        device_name = devices[args.device].name
        fingerprint = settings_fingerprint(settings, device_name, folders)
        print(f"Device: {device_name}")
        print(f"Folders: {', '.join(folders)}")
        print(f"Destination: {settings.dest_root}")

        for event in verify_transfer(device, folders, settings, fingerprint):
            if event.action == "_RESULT":
                status = event.source
                missing_count = int(event.reason or "0")
                continue
            _echo_event(log, event, quiet=args.quiet)
    except Exception as exc:
        append_event(log, TransferEvent(action="ERROR", source="Verify", reason=str(exc)))
        return 1
    finally:
        close_device(device)

    if status == "ready":
        print(f"\nVerify ready — all importable files found at destination.")
        return 0
    print(f"\nVerify blocked — {missing_count} file(s) missing or mismatched.")
    return 1


def _cmd_delete(args: argparse.Namespace) -> int:
    if not args.confirm_backup:
        print("Refusing to delete: pass --confirm-backup to acknowledge backup.", file=sys.stderr)
        return 1

    devices, device = _open_selected_device(args)
    log: list[str] = []
    stats = DeleteStats()
    try:
        folders = _resolve_folders(device, args)
        settings = _build_settings(args)
        device_name = devices[args.device].name
        fingerprint = settings_fingerprint(settings, device_name, folders)

        if not args.force_delete:
            status = "blocked"
            for event in verify_transfer(device, folders, settings, fingerprint):
                if event.action == "_RESULT":
                    status = event.source
            if status != "ready":
                print(
                    "Delete blocked: run verify first or pass --force-delete.",
                    file=sys.stderr,
                )
                return 1

        print(f"Device: {device_name}")
        print(f"Deleting folders: {', '.join(folders)}")

        for event, stats in delete_folders(
            device,
            folders,
            skip_trashed=settings.skip_trashed,
            skip_thumbnails=settings.skip_thumbnails,
            skip_screenshots=settings.skip_screenshots,
        ):
            _echo_event(log, event, quiet=args.quiet)
    except Exception as exc:
        append_event(log, TransferEvent(action="ERROR", source="Delete", reason=str(exc)))
        return 1
    finally:
        close_device(device)

    print(
        f"\nDone — {stats.folders_deleted} folder(s), "
        f"{stats.files_deleted} file(s), errors {stats.errors}"
    )
    return 0 if stats.errors == 0 else 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
