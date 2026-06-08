"""Shared operation state for cancellable background tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from transfer.importer import ImportStats
    from transfer.phone_cleanup import DeleteStats
    from transfer.preview import TransferCandidate


def _new_import_stats() -> ImportStats:
    from transfer.importer import ImportStats

    return ImportStats()


def _new_delete_stats() -> DeleteStats:
    from transfer.phone_cleanup import DeleteStats

    return DeleteStats()


@dataclass
class OperationControl:
    kind: str
    stop_requested: bool = False
    running: bool = True
    finished: bool = False
    error: str | None = None
    activity: dict[str, Any] = field(default_factory=dict)
    log_lines: list[str] = field(default_factory=list)
    import_stats: Any = field(default_factory=_new_import_stats)
    delete_stats: Any = field(default_factory=_new_delete_stats)
    verify_status: str | None = None
    verify_fingerprint: str = ""
    verify_missing_count: int = 0
    transfer_candidates: list[TransferCandidate] = field(default_factory=list)
    preview_fingerprint: str = ""
    invalidate_verify: bool = False
    delete_confirm_reset: bool = False

    def should_stop(self) -> bool:
        return self.stop_requested

    def request_stop(self) -> None:
        self.stop_requested = True

    def set_activity(self, **kwargs: object) -> None:
        self.activity.update(kwargs)

    def append_log(self, line: str, *, max_lines: int = 500) -> None:
        self.log_lines.append(line)
        if len(self.log_lines) > max_lines:
            del self.log_lines[: len(self.log_lines) - max_lines]
