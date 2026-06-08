"""Rename files from the old dot-separated time template to the new underscore style.

Old: {stem}-YYYY-MM-DD_HH.MM.SS.ext   (e.g. IMG_1234-2026-06-08_19.14.27.jpg)
New: {stem}-YYYY-MM-DD_HH_MM_SS.ext   (e.g. IMG_1234-2026-06-08_19_14_27.jpg)

Usage:
    python misc/rename_to_template.py [--root PATH] [--apply]

Example:
    python misc/rename_to_template.py --apply

Arguments:
    --root PATH   Root folder to scan recursively (default: D:\\Album-F).
    --apply       Perform renames. Without this flag, only show planned changes.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT = Path(r"D:\Album-F")

OLD_TEMPLATE_RE = re.compile(
    r"^(?P<stem>.+)-(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})_"
    r"(?P<hour>\d{2})\.(?P<minute>\d{2})\.(?P<second>\d{2})"
    r"(?P<ext>\.[A-Za-z0-9]+)$"
)


@dataclass(frozen=True)
class RenamePlan:
    source: Path
    target: Path


@dataclass
class RenameStats:
    scanned: int = 0
    matched: int = 0
    renamed: int = 0
    skipped_exists: int = 0
    errors: int = 0


def new_name_from_old(filename: str) -> str | None:
    match = OLD_TEMPLATE_RE.match(filename)
    if not match:
        return None
    parts = match.groupdict()
    return (
        f"{parts['stem']}-{parts['year']}-{parts['month']}-{parts['day']}_"
        f"{parts['hour']}_{parts['minute']}_{parts['second']}{parts['ext']}"
    )


def collect_renames(root: Path) -> list[RenamePlan]:
    plans: list[RenamePlan] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        new_name = new_name_from_old(path.name)
        if new_name is None:
            continue
        target = path.with_name(new_name)
        if target == path:
            continue
        plans.append(RenamePlan(source=path, target=target))
    return sorted(plans, key=lambda plan: str(plan.source).lower())


def apply_renames(plans: list[RenamePlan], *, apply: bool) -> RenameStats:
    stats = RenameStats(scanned=len(plans), matched=len(plans))
    for plan in plans:
        if plan.target.exists():
            print(f"skip (exists): {plan.source} -> {plan.target.name}", file=sys.stderr)
            stats.skipped_exists += 1
            continue
        if apply:
            try:
                plan.source.rename(plan.target)
            except OSError as exc:
                print(f"error: {plan.source} -> {plan.target.name}: {exc}", file=sys.stderr)
                stats.errors += 1
                continue
            print(f"renamed: {plan.source} -> {plan.target.name}")
            stats.renamed += 1
        else:
            print(f"would rename: {plan.source} -> {plan.target.name}")
    return stats


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rename files using the old HH.MM.SS template suffix to HH_MM_SS "
            f"under {DEFAULT_ROOT} (recursive)."
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"Root folder to scan (default: {DEFAULT_ROOT}).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform renames. Without this flag, only show planned changes.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 1

    plans = collect_renames(root)
    if not plans:
        print(f"No files matched the old template under {root}")
        return 0

    mode = "Applying" if args.apply else "Dry run"
    print(f"{mode}: {len(plans)} file(s) under {root}")
    stats = apply_renames(plans, apply=args.apply)

    print(
        f"done: matched={stats.matched} renamed={stats.renamed} "
        f"skipped_exists={stats.skipped_exists} errors={stats.errors}"
    )
    if not args.apply and stats.matched:
        print("Re-run with --apply to rename files.")
    return 1 if stats.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
