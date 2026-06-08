"""Rename files by replacing dots in the name with underscores.

Dots in the file extension are preserved. Consecutive dots in the stem become
a single underscore.

Examples:
    my.file.name.jpg       -> my_file_name.jpg
    IMG-2026.06.08_19.14.27.png -> IMG-2026_06_08_19_14_27.png
    file..name.txt         -> file_name.txt

Usage:
    python misc/rename_dot_to_underscore.py [--root PATH] [--apply]

Example:
    python misc/rename_dot_to_underscore.py --apply

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

STEM_DOTS_RE = re.compile(r"\.+")


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


def new_name_without_stem_dots(filename: str) -> str | None:
    if "." not in filename:
        return None
    suffix = Path(filename).suffix
    stem = filename[: -len(suffix)] if suffix else filename
    new_stem = STEM_DOTS_RE.sub("_", stem)
    if new_stem == stem:
        return None
    return f"{new_stem}{suffix}"


def collect_renames(root: Path) -> list[RenamePlan]:
    plans: list[RenamePlan] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        new_name = new_name_without_stem_dots(path.name)
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
            "Replace dots in file names with underscores, preserving the "
            f"extension dot, under {DEFAULT_ROOT} (recursive)."
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
        print(f"No files with stem dots found under {root}")
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
