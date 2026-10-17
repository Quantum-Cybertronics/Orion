"""The event log: one entry per thing that happened during a run."""

from dataclasses import dataclass
from typing import Protocol

# What each type means (the page picks an icon from it):
#   run_started    the run began
#   step_started   a step began
#   step_finished  a step succeeded
#   step_failed    a step failed (the run stops, unless the step was optional)
#   step_skipped   an optional step failed and the run carried on
#   tool_call      a step used a tool (e.g. the reader opened a file)
#   proposal       changes waiting for the user's approval (detail is the diff)
#   info / warning a note from inside a step
#   run_finished   the run ended (completed, failed or cancelled)
EVENT_TYPES = (
    "run_started",
    "step_started",
    "step_finished",
    "step_failed",
    "step_skipped",
    "tool_call",
    "proposal",
    "info",
    "warning",
    "run_finished",
)


@dataclass(frozen=True)
class Event:
    type: str
    title: str                       # short, human readable
    role: str | None = None          # who did it: supervisor, reader, editor...
    detail: str | None = None        # longer text, shown under the title
    duration_ms: int | None = None   # how long a step took (step_* events)
    elapsed_ms: int = 0              # time since the run started


class EventSink(Protocol):
    """Anything that can receive events (the database, or a list in tests)."""

    def emit(self, event: Event) -> None: ...


class ListSink:
    """Keeps events in memory. Used by tests."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)
