"""Find near-duplicate videos using a cascading match pipeline.

Candidates are narrowed in order: file date, similar file names, file size,
then frame sampling with perceptual hashing. By default the script runs in
dry-run mode; use --apply to move extras into a path-mirrored quarantine.

Usage:
    python misc/de-duplicate_videos.py [--root PATH] [--limit-groups N]

Example:
    python misc/de-duplicate_videos.py --limit-groups 3
    python misc/de-duplicate_videos.py --apply

Arguments:
    --root PATH              Root folder to scan recursively (default: D:\\Album-F).
    --quarantine PATH        Quarantine root (default: {root}/_duplicate_videos).
    --samples N              Frames to sample per video (default: 5).
    --threshold N            Max per-frame Hamming distance (default: 8).
    --duration-tolerance SEC Max duration difference for comparison (default: 2).
    --date-tolerance-days N  Max days between file dates (default: 1).
    --size-tolerance RATIO   Max file size difference ratio (default: 0.15).
    --name-similarity RATIO  Min stem similarity 0-1; 0 disables (default: 0).
    --keep STRATEGY          Which copy to keep (default: shortest-path).
    --limit-groups N         Stop after N duplicate groups (for testing).
    --corrupt-log PATH       Log corrupt/unreadable files (default: D:\\Album-F\\corrupt_videos.log).
    --apply                  Move duplicates to quarantine.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path

import imagehash  # pyright: ignore[reportMissingImports]
from PIL import Image  # pyright: ignore[reportMissingImports]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from transfer.datetime_meta import capture_datetime_from_filename
from transfer.filters import VIDEO_EXTENSIONS

DEFAULT_ROOT = Path(r"D:\Album-F")
DEFAULT_QUARANTINE_NAME = "_duplicate_videos"
DEFAULT_CORRUPT_LOG_NAME = "corrupt_videos.log"
DEFAULT_CORRUPT_LOG = DEFAULT_ROOT / DEFAULT_CORRUPT_LOG_NAME
DEFAULT_SAMPLES = 5
DEFAULT_THRESHOLD = 10
DEFAULT_DURATION_TOLERANCE = 5.0
DEFAULT_DATE_TOLERANCE_DAYS = 1
DEFAULT_SIZE_TOLERANCE = 0.15
DEFAULT_NAME_SIMILARITY = 0.0
SIZE_BUCKET_BYTES = 1024 * 1024
KEEP_STRATEGIES = ("shortest-path", "oldest", "newest", "largest")
SAMPLE_FRACTIONS = (0.1, 0.3, 0.5, 0.7, 0.9)
RENAME_SUFFIX_RE = re.compile(
    r"-\d{4}-\d{2}-\d{2}_\d{2}[._]\d{2}[._]\d{2}$",
)


@dataclass(frozen=True)
class FileInfo:
    path: Path
    size: int
    file_date: date
    normalized_stem: str


@dataclass(frozen=True)
class VideoMetadata:
    duration: float
    width: int
    height: int
    size: int


@dataclass(frozen=True)
class MovePlan:
    source: Path
    target: Path
    keeper: Path


@dataclass
class CorruptFileLog:
    log_path: Path
    logged: set[str] = field(default_factory=set)

    def write(self, path: Path, reason: str) -> None:
        full_path = str(path.resolve())
        if full_path in self.logged:
            return
        self.logged.add(full_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{full_path}\n")
        print(f"warn: corrupt file logged: {full_path} ({reason})", file=sys.stderr)


@dataclass
class DedupStats:
    scanned: int = 0
    fingerprinted: int = 0
    groups: int = 0
    duplicates: int = 0
    bytes_wasted: int = 0
    moved: int = 0
    skipped_exists: int = 0
    errors: int = 0
    stopped_early: bool = False


@dataclass
class UnionFind:
    parent: dict[Path, Path] = field(default_factory=dict)

    def find(self, item: Path) -> Path:
        if item not in self.parent:
            self.parent[item] = item
        root = self.parent[item]
        if root != item:
            self.parent[item] = self.find(root)
        return self.parent[item]

    def union(self, left: Path, right: Path) -> None:
        root_left = self.find(left)
        root_right = self.find(right)
        if root_left != root_right:
            self.parent[root_right] = root_left

    def groups(self) -> list[list[Path]]:
        grouped: dict[Path, list[Path]] = defaultdict(list)
        for item in self.parent:
            grouped[self.find(item)].append(item)
        return [sorted(members, key=lambda path: str(path).lower()) for members in grouped.values()]


def check_ffmpeg_tools() -> str | None:
    for tool in ("ffprobe", "ffmpeg"):
        try:
            subprocess.run(
                [tool, "-version"],
                capture_output=True,
                check=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return f"error: {tool} not found on PATH (install ffmpeg and add to PATH)"
    return None


def is_video_path(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def collect_videos(root: Path, quarantine_root: Path) -> list[Path]:
    videos: list[Path] = []
    quarantine_resolved = quarantine_root.resolve()
    for path in root.rglob("*"):
        if not path.is_file() or not is_video_path(path):
            continue
        try:
            if quarantine_resolved in path.resolve().parents:
                continue
        except OSError:
            continue
        videos.append(path)
    return sorted(videos, key=lambda item: str(item).lower())


def run_ffprobe(path: Path, corrupt_log: CorruptFileLog | None = None) -> VideoMetadata | None:
    command = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=True,
            timeout=60,
            text=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"warn: ffprobe failed for {path}: {exc}", file=sys.stderr)
        if corrupt_log is not None:
            corrupt_log.write(path, "ffprobe failed")
        return None

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"warn: ffprobe returned invalid JSON for {path}", file=sys.stderr)
        if corrupt_log is not None:
            corrupt_log.write(path, "ffprobe invalid json")
        return None

    duration = 0.0
    format_info = payload.get("format") or {}
    if "duration" in format_info:
        try:
            duration = float(format_info["duration"])
        except (TypeError, ValueError):
            duration = 0.0

    width = 0
    height = 0
    for stream in payload.get("streams") or []:
        if stream.get("codec_type") != "video":
            continue
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        break

    try:
        size = path.stat().st_size
    except OSError:
        size = 0

    if duration <= 0 or width <= 0 or height <= 0:
        print(f"warn: incomplete metadata for {path}", file=sys.stderr)
        if corrupt_log is not None:
            corrupt_log.write(path, "incomplete metadata")
        return None

    return VideoMetadata(duration=duration, width=width, height=height, size=size)


def normalize_stem(filename: str) -> str:
    stem = Path(filename).stem
    return RENAME_SUFFIX_RE.sub("", stem).lower()


def file_date_for(path: Path) -> date:
    parsed = capture_datetime_from_filename(path.name)
    if parsed is not None:
        return parsed.date()
    return datetime.fromtimestamp(path.stat().st_mtime).date()


def collect_file_info(
    videos: list[Path],
    corrupt_log: CorruptFileLog | None = None,
) -> list[FileInfo]:
    items: list[FileInfo] = []
    total = len(videos)
    for index, path in enumerate(videos, start=1):
        print_progress(index, total)
        try:
            stat = path.stat()
        except OSError as exc:
            print(f"warn: could not stat {path}: {exc}", file=sys.stderr)
            if corrupt_log is not None:
                corrupt_log.write(path, "could not stat")
            continue
        items.append(
            FileInfo(
                path=path,
                size=stat.st_size,
                file_date=file_date_for(path),
                normalized_stem=normalize_stem(path.name),
            )
        )
    return items


def names_similar(left: FileInfo, right: FileInfo, min_ratio: float) -> bool:
    if left.normalized_stem == right.normalized_stem:
        return True
    if left.normalized_stem in right.normalized_stem or right.normalized_stem in left.normalized_stem:
        return True
    return SequenceMatcher(None, left.normalized_stem, right.normalized_stem).ratio() >= min_ratio


def dates_compatible(left: FileInfo, right: FileInfo, tolerance_days: int) -> bool:
    return abs((left.file_date - right.file_date).days) <= tolerance_days


def sizes_compatible(left_size: int, right_size: int, tolerance_ratio: float) -> bool:
    if left_size == right_size:
        return True
    larger = max(left_size, right_size)
    if larger == 0:
        return False
    return abs(left_size - right_size) / larger <= tolerance_ratio


def is_candidate_pair(
    left: FileInfo,
    right: FileInfo,
    *,
    date_tolerance_days: int,
    size_tolerance: float,
    name_similarity: float,
) -> bool:
    if not dates_compatible(left, right, date_tolerance_days):
        return False
    if not sizes_compatible(left.size, right.size, size_tolerance):
        return False
    if name_similarity <= 0:
        return True
    return names_similar(left, right, name_similarity)


def size_bucket(size: int) -> int:
    return max(size, 1) // SIZE_BUCKET_BYTES


def bucket_by_size_mb(items: list[FileInfo]) -> dict[int, list[FileInfo]]:
    buckets: dict[int, list[FileInfo]] = defaultdict(list)
    for item in items:
        buckets[size_bucket(item.size)].append(item)
    return buckets


def iter_prefilter_groups(
    size_buckets: dict[int, list[FileInfo]],
) -> list[list[FileInfo]]:
    groups: list[list[FileInfo]] = []
    for bucket_mb in sorted(size_buckets):
        bucket_items = size_buckets[bucket_mb]
        if len(bucket_items) >= 2:
            groups.append(bucket_items)
        next_bucket = size_buckets.get(bucket_mb + 1)
        if next_bucket:
            groups.append(
                sorted(
                    bucket_items + next_bucket,
                    key=lambda item: str(item.path).lower(),
                )
            )
    return groups


def _count_candidate_paths(
    size_buckets: dict[int, list[FileInfo]],
    *,
    date_tolerance_days: int,
    size_tolerance: float,
    name_similarity: float,
) -> int:
    paths: set[Path] = set()
    for group in iter_prefilter_groups(size_buckets):
        for left, right in find_candidate_pairs(
            group,
            date_tolerance_days=date_tolerance_days,
            size_tolerance=size_tolerance,
            name_similarity=name_similarity,
        ):
            paths.add(left.path)
            paths.add(right.path)
    return len(paths)


def find_candidate_pairs(
    group: list[FileInfo],
    *,
    date_tolerance_days: int,
    size_tolerance: float,
    name_similarity: float,
) -> list[tuple[FileInfo, FileInfo]]:
    pairs: list[tuple[FileInfo, FileInfo]] = []
    ordered = sorted(group, key=lambda item: str(item.path).lower())
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            if is_candidate_pair(
                left,
                right,
                date_tolerance_days=date_tolerance_days,
                size_tolerance=size_tolerance,
                name_similarity=name_similarity,
            ):
                pairs.append((left, right))
    return pairs


def sample_timestamps(duration: float, sample_count: int) -> list[float]:
    if sample_count <= 0:
        return []
    if sample_count == 1:
        return [max(duration * 0.5, 0.0)]
    fractions = list(SAMPLE_FRACTIONS)
    if sample_count != len(fractions):
        step = 1.0 / (sample_count + 1)
        fractions = [step * (index + 1) for index in range(sample_count)]
    max_offset = max(duration - 0.1, 0.0)
    return [min(duration * fraction, max_offset) for fraction in fractions]


def extract_frame_png(path: Path, timestamp_sec: float) -> bytes | None:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{timestamp_sec:.3f}",
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "png",
        "-",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=True,
            timeout=120,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"warn: frame extract failed for {path} at {timestamp_sec:.2f}s: {exc}", file=sys.stderr)
        return None
    if not result.stdout:
        return None
    return result.stdout


def video_fingerprint(
    path: Path,
    sample_count: int,
    meta: VideoMetadata | None = None,
    corrupt_log: CorruptFileLog | None = None,
) -> tuple[imagehash.ImageHash, ...] | None:
    resolved_meta = meta if meta is not None else run_ffprobe(path, corrupt_log)
    if resolved_meta is None:
        return None

    hashes: list[imagehash.ImageHash] = []
    for timestamp in sample_timestamps(resolved_meta.duration, sample_count):
        png_bytes = extract_frame_png(path, timestamp)
        if png_bytes is None:
            continue
        try:
            with Image.open(BytesIO(png_bytes)) as image:
                hashes.append(imagehash.phash(image))
        except OSError as exc:
            print(f"warn: could not hash frame for {path}: {exc}", file=sys.stderr)

    if not hashes:
        print(f"warn: no frames fingerprinted for {path}", file=sys.stderr)
        if corrupt_log is not None:
            corrupt_log.write(path, "no frames fingerprinted")
        return None
    return tuple(hashes)


def fingerprints_similar(
    left: tuple[imagehash.ImageHash, ...],
    right: tuple[imagehash.ImageHash, ...],
    threshold: int,
) -> bool:
    pair_count = min(len(left), len(right))
    if pair_count == 0:
        return False
    distances = [left[index] - right[index] for index in range(pair_count)]
    return max(distances) <= threshold


def durations_compatible(
    left: VideoMetadata,
    right: VideoMetadata,
    tolerance: float,
) -> bool:
    return abs(left.duration - right.duration) <= tolerance


def print_progress(current: int, total: int) -> None:
    print(f"Analyzed {current}/{total} videos", file=sys.stderr)


def _register_new_groups(
    union_find: UnionFind,
    seen_roots: set[Path],
    discovery_order: list[Path],
) -> None:
    for members in union_find.groups():
        if len(members) < 2:
            continue
        root = union_find.find(members[0])
        if root in seen_roots:
            continue
        seen_roots.add(root)
        discovery_order.append(root)


def find_duplicate_groups(
    items: list[FileInfo],
    *,
    sample_count: int,
    threshold: int,
    duration_tolerance: float,
    date_tolerance_days: int,
    size_tolerance: float,
    name_similarity: float,
    limit_groups: int | None,
    corrupt_log: CorruptFileLog | None = None,
) -> tuple[list[list[Path]], bool]:
    size_buckets = bucket_by_size_mb(items)

    union_find = UnionFind()
    fingerprint_cache: dict[Path, tuple[imagehash.ImageHash, ...]] = {}
    meta_cache: dict[Path, VideoMetadata | None] = {}
    seen_roots: set[Path] = set()
    discovery_order: list[Path] = []
    stopped_early = False
    fingerprinted = 0
    processed_pairs: set[frozenset[Path]] = set()
    fingerprint_total = _count_candidate_paths(
        size_buckets,
        date_tolerance_days=date_tolerance_days,
        size_tolerance=size_tolerance,
        name_similarity=name_similarity,
    )

    for group in iter_prefilter_groups(size_buckets):
        if len(group) < 2:
            continue

        for left_info, right_info in find_candidate_pairs(
            group,
            date_tolerance_days=date_tolerance_days,
            size_tolerance=size_tolerance,
            name_similarity=name_similarity,
        ):
            left_path = left_info.path
            right_path = right_info.path
            pair_key = frozenset({left_path, right_path})
            if pair_key in processed_pairs:
                continue
            processed_pairs.add(pair_key)

            if left_path not in meta_cache:
                meta_cache[left_path] = run_ffprobe(left_path, corrupt_log)
            if right_path not in meta_cache:
                meta_cache[right_path] = run_ffprobe(right_path, corrupt_log)
            left_meta = meta_cache[left_path]
            right_meta = meta_cache[right_path]
            if left_meta is None or right_meta is None:
                continue
            if not durations_compatible(left_meta, right_meta, duration_tolerance):
                continue

            for path, meta in ((left_path, left_meta), (right_path, right_meta)):
                if path not in fingerprint_cache:
                    fingerprinted += 1
                    if fingerprint_total:
                        print_progress(fingerprinted, fingerprint_total)
                    fingerprint_cache[path] = video_fingerprint(
                        path,
                        sample_count,
                        meta,
                        corrupt_log,
                    )

            left_fp = fingerprint_cache.get(left_path)
            right_fp = fingerprint_cache.get(right_path)
            if left_fp is None or right_fp is None:
                continue
            if fingerprints_similar(left_fp, right_fp, threshold):
                union_find.union(left_path, right_path)

        _register_new_groups(union_find, seen_roots, discovery_order)
        if limit_groups is not None and len(discovery_order) >= limit_groups:
            stopped_early = True
            break

    grouped_by_root = {union_find.find(members[0]): members for members in union_find.groups() if len(members) >= 2}
    ordered_groups = [
        grouped_by_root[root]
        for root in discovery_order
        if root in grouped_by_root
    ]
    if limit_groups is not None:
        ordered_groups = ordered_groups[:limit_groups]
    else:
        extra_roots = [
            root
            for root in sorted(grouped_by_root, key=lambda item: str(item).lower())
            if root not in discovery_order
        ]
        ordered_groups.extend(grouped_by_root[root] for root in extra_roots)

    return ordered_groups, stopped_early


def choose_keeper(group: list[Path], strategy: str) -> Path:
    if strategy == "shortest-path":
        return min(group, key=lambda path: (len(str(path)), str(path).lower()))
    if strategy == "oldest":
        return min(group, key=lambda path: (path.stat().st_mtime, str(path).lower()))
    if strategy == "newest":
        return max(group, key=lambda path: (path.stat().st_mtime, str(path).lower()))
    if strategy == "largest":
        return max(group, key=lambda path: (path.stat().st_size, str(path).lower()))
    raise ValueError(f"unsupported keep strategy: {strategy}")


def quarantine_path_for(source: Path, scan_root: Path, quarantine_root: Path) -> Path:
    return quarantine_root / source.resolve().relative_to(scan_root.resolve())


def plan_quarantine_moves(
    groups: list[list[Path]],
    *,
    scan_root: Path,
    quarantine_root: Path,
    keep_strategy: str,
) -> list[MovePlan]:
    plans: list[MovePlan] = []
    for group in groups:
        keeper = choose_keeper(group, keep_strategy)
        for path in group:
            if path == keeper:
                continue
            plans.append(
                MovePlan(
                    source=path,
                    target=quarantine_path_for(path, scan_root, quarantine_root),
                    keeper=keeper,
                )
            )
    return sorted(plans, key=lambda plan: str(plan.source).lower())


def apply_moves(plans: list[MovePlan], *, apply: bool) -> DedupStats:
    stats = DedupStats()
    stats.duplicates = len(plans)
    stats.bytes_wasted = sum(plan.source.stat().st_size for plan in plans if plan.source.exists())

    for plan in plans:
        if plan.target.exists():
            print(
                f"skip (exists): {plan.source} -> {plan.target}",
                file=sys.stderr,
            )
            stats.skipped_exists += 1
            continue
        if apply:
            try:
                plan.target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(plan.source), str(plan.target))
            except OSError as exc:
                print(
                    f"error: {plan.source} -> {plan.target}: {exc}",
                    file=sys.stderr,
                )
                stats.errors += 1
                continue
            print(f"moved: {plan.source} -> {plan.target}")
            stats.moved += 1
        else:
            print(f"would move: {plan.source} -> {plan.target}")
    return stats


def report_groups(groups: list[list[Path]], keep_strategy: str) -> None:
    for index, group in enumerate(groups, start=1):
        keeper = choose_keeper(group, keep_strategy)
        duplicate_paths = [path for path in group if path != keeper]
        wasted = sum(path.stat().st_size for path in duplicate_paths)
        print(f"group {index}: keeper={keeper}")
        for path in duplicate_paths:
            print(f"  duplicate: {path}")
        print(f"  reclaimable: {wasted} bytes")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find near-duplicate videos under a folder. Loosely pre-filters by date, "
            "size, and optionally name, then confirms with frame hashing. "
            f"Default root: {DEFAULT_ROOT}."
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=f"Root folder to scan (default: {DEFAULT_ROOT}).",
    )
    parser.add_argument(
        "--quarantine",
        type=Path,
        default=None,
        help=f"Quarantine root (default: {{root}}/{DEFAULT_QUARANTINE_NAME}).",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=DEFAULT_SAMPLES,
        help=f"Frames sampled per video (default: {DEFAULT_SAMPLES}).",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_THRESHOLD,
        help=f"Max per-frame Hamming distance (default: {DEFAULT_THRESHOLD}).",
    )
    parser.add_argument(
        "--duration-tolerance",
        type=float,
        default=DEFAULT_DURATION_TOLERANCE,
        help=f"Max duration difference in seconds (default: {DEFAULT_DURATION_TOLERANCE}).",
    )
    parser.add_argument(
        "--date-tolerance-days",
        type=int,
        default=DEFAULT_DATE_TOLERANCE_DAYS,
        help=f"Max days between file dates (default: {DEFAULT_DATE_TOLERANCE_DAYS}).",
    )
    parser.add_argument(
        "--size-tolerance",
        type=float,
        default=DEFAULT_SIZE_TOLERANCE,
        help=f"Max file size difference ratio (default: {DEFAULT_SIZE_TOLERANCE}).",
    )
    parser.add_argument(
        "--name-similarity",
        type=float,
        default=DEFAULT_NAME_SIMILARITY,
        help=(
            f"Min stem similarity ratio 0-1; 0 disables name filter "
            f"(default: {DEFAULT_NAME_SIMILARITY})."
        ),
    )
    parser.add_argument(
        "--keep",
        choices=KEEP_STRATEGIES,
        default="shortest-path",
        help="Which file to keep in each duplicate group.",
    )
    parser.add_argument(
        "--limit-groups",
        type=int,
        default=None,
        metavar="N",
        help="Stop after N duplicate groups are found (for testing).",
    )
    parser.add_argument(
        "--corrupt-log",
        type=Path,
        default=None,
        help=f"Corrupt file log path (default: {DEFAULT_CORRUPT_LOG}).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Move duplicates to quarantine. Without this flag, only show planned moves.",
    )
    return parser.parse_args(argv)


def print_corrupt_summary(corrupt_log: CorruptFileLog) -> None:
    if not corrupt_log.logged:
        return
    print(
        f"corrupt_files={len(corrupt_log.logged)} logged to {corrupt_log.log_path}",
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    tool_error = check_ffmpeg_tools()
    if tool_error:
        print(tool_error, file=sys.stderr)
        return 1

    scan_root = args.root.resolve()
    if not scan_root.is_dir():
        print(f"error: not a directory: {scan_root}", file=sys.stderr)
        return 1

    quarantine_root = (
        args.quarantine.resolve()
        if args.quarantine is not None
        else (scan_root / DEFAULT_QUARANTINE_NAME).resolve()
    )
    corrupt_log_path = (
        args.corrupt_log.resolve()
        if args.corrupt_log is not None
        else DEFAULT_CORRUPT_LOG.resolve()
    )
    corrupt_log = CorruptFileLog(log_path=corrupt_log_path)

    videos = collect_videos(scan_root, quarantine_root)
    if not videos:
        print(f"No videos found under {scan_root}")
        return 0

    print(f"Collecting file info for {len(videos)} video(s) under {scan_root}", file=sys.stderr)
    items = collect_file_info(videos, corrupt_log)
    if len(items) < 2:
        print(f"Not enough readable videos to compare under {scan_root}")
        print_corrupt_summary(corrupt_log)
        return 0

    print(
        "Matching by date, name, and size; fingerprinting frame samples for candidates...",
        file=sys.stderr,
    )
    groups, stopped_early = find_duplicate_groups(
        items,
        sample_count=args.samples,
        threshold=args.threshold,
        duration_tolerance=args.duration_tolerance,
        date_tolerance_days=args.date_tolerance_days,
        size_tolerance=args.size_tolerance,
        name_similarity=args.name_similarity,
        limit_groups=args.limit_groups,
        corrupt_log=corrupt_log,
    )

    if not groups:
        print(f"No duplicate groups found under {scan_root}")
        print_corrupt_summary(corrupt_log)
        return 0

    mode = "Applying" if args.apply else "Dry run"
    print(f"{mode}: {len(groups)} duplicate group(s) under {scan_root}")
    report_groups(groups, args.keep)

    plans = plan_quarantine_moves(
        groups,
        scan_root=scan_root,
        quarantine_root=quarantine_root,
        keep_strategy=args.keep,
    )
    move_stats = apply_moves(plans, apply=args.apply)

    print(
        "done: "
        f"scanned={len(videos)} groups={len(groups)} duplicates={move_stats.duplicates} "
        f"bytes={move_stats.bytes_wasted} moved={move_stats.moved} "
        f"skipped_exists={move_stats.skipped_exists} errors={move_stats.errors}"
    )

    if stopped_early and args.limit_groups is not None:
        print(
            f"Stopped after {len(groups)} duplicate group(s) "
            f"(--limit-groups {args.limit_groups}). "
            "Re-run without --limit-groups for a full scan."
        )

    if not args.apply and plans:
        print("Re-run with --apply to move duplicates.")
    print_corrupt_summary(corrupt_log)
    return 1 if move_stats.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
