"""Transfer log events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class TransferEvent:
    action: str
    source: str = ""
    dest: str = ""
    reason: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))

    def format_line(self) -> str:
        if self.action == "COPY" and self.dest:
            return (
                f"[{self.timestamp}] COPY  {self.source}\n"
                f"         ->  {self.dest}"
            )
        if self.reason:
            return f"[{self.timestamp}] {self.action}  {self.source}  ({self.reason})"
        return f"[{self.timestamp}] {self.action}  {self.source}"


def append_event(
    log: list[str],
    event: TransferEvent,
    max_lines: int = 500,
    *,
    echo_terminal: bool = True,
) -> None:
    line = event.format_line()
    log.append(line)
    if len(log) > max_lines:
        del log[: len(log) - max_lines]
    if echo_terminal:
        print(line, flush=True)
