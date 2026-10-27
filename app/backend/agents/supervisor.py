"""The supervisor: runs steps in order, enforces limits, honours cancellation.

This is plain code, not a model. It decides what runs next from a fixed list
of steps, stops when a step fails or a limit is hit, and writes every
decision to the event log. Steps may call the AI, but they cannot change the
plan or exceed the limits.
"""

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from app.backend.agents.events import Event, EventSink

logger = logging.getLogger("orion.agents")


class RunCancelled(Exception):
    """Raised inside a step when the user asked to cancel the run."""


class StepFailed(Exception):
    """A step could not do its job. The message is shown to the user."""


APPROVAL_TIMEOUT_SECONDS = 30 * 60   # how long a run waits for the user's decision
APPROVAL_POLL_SECONDS = 0.25


class AcknowledgementRequired(Exception):
    """Applying was chosen, but the user has not confirmed the high-risk findings."""


class ApprovalGate:
    """Lets a paused run receive the user's decision from another thread."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._waiting = False
        self.decision: str | None = None
        self.needs_acknowledgement = False

    def open(self, require_acknowledgement: bool = False) -> None:
        with self._lock:
            self.decision = None
            self._event.clear()
            self._waiting = True
            self.needs_acknowledgement = require_acknowledgement

    def close(self) -> None:
        with self._lock:
            self._waiting = False

    @property
    def waiting(self) -> bool:
        return self._waiting

    def decide(self, decision: str, acknowledged: bool = False) -> bool:
        """Record the decision. False if nothing is waiting or it was already made.

        Raises ``AcknowledgementRequired`` if applying while high-risk findings
        have not been explicitly confirmed. Rejecting never needs that.
        """
        with self._lock:
            if not self._waiting or self.decision is not None:
                return False

            if decision == "apply" and self.needs_acknowledgement and not acknowledged:
                raise AcknowledgementRequired

            self.decision = decision
            self._event.set()

            return True

    def wait(self, seconds: float) -> bool:
        return self._event.wait(seconds)


@dataclass(frozen=True)
class Limits:
    max_steps: int = 25
    max_seconds: float = 900.0


@dataclass
class RunResult:
    status: str                  # "completed" | "failed" | "cancelled"
    summary: str | None = None
    error: str | None = None


class StepContext:
    """What a step can see and do while it runs."""

    def __init__(self, supervisor: "Supervisor", step: "Step") -> None:
        self._supervisor = supervisor
        self._step = step

    @property
    def cancel(self) -> threading.Event:
        return self._supervisor.cancel

    @property
    def state(self) -> dict:
        """Values steps share with later steps in the same run."""
        return self._supervisor.state

    def check_cancelled(self) -> None:
        if self._supervisor.cancel.is_set():
            raise RunCancelled

    def seconds_left(self) -> float:
        """Time left before the run's overall limit."""
        return self._supervisor.seconds_left()

    def proposal(self, title: str, diff: str) -> None:
        """Show changes waiting for the user's decision (``diff`` is the detail)."""
        self._supervisor.emit("proposal", title, role=self._step.role, detail=diff)

    def check(self, title: str, detail: str | None = None) -> None:
        """Record the result of a check (``detail`` is JSON the page can read)."""
        self._supervisor.emit("check", title, role=self._step.role, detail=detail)

    def wait_for_approval(
        self,
        timeout_seconds: float = APPROVAL_TIMEOUT_SECONDS,
        require_acknowledgement: bool = False,
    ) -> str:
        """Pause the run until the user decides. Returns "apply" or "reject".

        The run shows as "awaiting approval" meanwhile. Cancelling raises
        ``RunCancelled``; running out of time raises ``StepFailed``. The time
        spent waiting does not count against the run's own time limit.
        """
        supervisor = self._supervisor
        gate = supervisor.gate

        if gate is None:
            raise StepFailed("this run cannot ask for approval")

        gate.open(require_acknowledgement)
        supervisor.set_status("awaiting_approval")
        began = supervisor.clock()

        try:
            while True:
                if supervisor.cancel.is_set():
                    raise RunCancelled

                if gate.wait(APPROVAL_POLL_SECONDS):
                    break

                if supervisor.clock() - began >= timeout_seconds:
                    raise StepFailed(
                        f"no decision was made within {int(timeout_seconds // 60)} minutes, "
                        "so nothing was changed"
                    )
        finally:
            gate.close()
            supervisor.credit_pause(supervisor.clock() - began)
            supervisor.set_status("running")

        return gate.decision or "reject"

    def info(self, title: str, detail: str | None = None) -> None:
        self._supervisor.emit("info", title, role=self._step.role, detail=detail)

    def warn(self, title: str, detail: str | None = None) -> None:
        self._supervisor.emit("warning", title, role=self._step.role, detail=detail)

    def tool(self, title: str, detail: str | None = None, duration_ms: int | None = None) -> None:
        """Record that the step used a tool (opened a file, ran a search...)."""
        self._supervisor.emit(
            "tool_call", title, role=self._step.role, detail=detail, duration_ms=duration_ms
        )


@dataclass
class Step:
    name: str
    title: str
    fn: Callable[[StepContext], str | None]   # returns optional result text
    role: str = "supervisor"
    optional: bool = False   # if it fails, note it and carry on


@dataclass
class Supervisor:
    sink: EventSink
    cancel: threading.Event
    limits: Limits = field(default_factory=Limits)
    clock: Callable[[], float] = time.monotonic
    state: dict = field(default_factory=dict)
    gate: ApprovalGate | None = None                        # set for runs that may ask the user
    status_hook: Callable[[str], None] | None = None        # told when the run pauses/resumes

    _start: float = 0.0
    _paused: float = 0.0   # time spent waiting for the user (not counted against the limit)

    # -- events ------------------------------------------------------------

    def elapsed_ms(self) -> int:
        return int((self.clock() - self._start) * 1000)

    def seconds_left(self) -> float:
        return self.limits.max_seconds - (self.clock() - self._start - self._paused)

    def credit_pause(self, seconds: float) -> None:
        """Do not count time spent waiting for the user against the time limit."""
        self._paused += max(0.0, seconds)

    def set_status(self, status: str) -> None:
        if self.status_hook is None:
            return

        try:
            self.status_hook(status)
        except Exception:
            logger.exception("Could not update the run's status to %r", status)

    def emit(
        self,
        type: str,
        title: str,
        *,
        role: str | None = "supervisor",
        detail: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        self.sink.emit(
            Event(
                type=type,
                title=title,
                role=role,
                detail=detail,
                duration_ms=duration_ms,
                elapsed_ms=self.elapsed_ms(),
            )
        )

    # -- the run -----------------------------------------------------------

    def run(self, steps: Sequence[Step], title: str = "Run") -> RunResult:
        steps = list(steps)
        total = len(steps)

        self._start = self.clock()
        self.emit("run_started", title, detail=f"{total} step{'' if total == 1 else 's'} planned")

        if total > self.limits.max_steps:
            return self._finish(
                "failed",
                error=f"The plan has {total} steps, more than the limit of {self.limits.max_steps}.",
            )

        finished = 0
        skipped = 0

        for number, step in enumerate(steps, start=1):
            if self.cancel.is_set():
                return self._finish("cancelled")

            if self.seconds_left() <= 0:
                return self._finish(
                    "failed",
                    error=f"The time limit of {int(self.limits.max_seconds)} seconds was reached.",
                )

            self.emit("step_started", f"Step {number}/{total}: {step.title}", role=step.role)

            began = self.clock()
            failure: str | None = None
            result: str | None = None

            try:
                result = step.fn(StepContext(self, step))
            except RunCancelled:
                self.emit(
                    "step_failed",
                    f"{step.title}: cancelled",
                    role=step.role,
                    duration_ms=self._ms_since(began),
                )
                return self._finish("cancelled")
            except StepFailed as exc:
                failure = str(exc) or "failed"
            except Exception as exc:
                logger.exception("Step %r crashed", step.name)
                failure = f"unexpected error ({type(exc).__name__}: {exc})"

            duration = self._ms_since(began)

            if failure is None:
                finished += 1
                self.emit(
                    "step_finished",
                    f"{step.title}: done",
                    role=step.role,
                    detail=result,
                    duration_ms=duration,
                )
            elif step.optional:
                skipped += 1
                self.emit(
                    "step_skipped",
                    f"{step.title}: {failure} (carrying on)",
                    role=step.role,
                    duration_ms=duration,
                )
            else:
                self.emit(
                    "step_failed",
                    f"{step.title}: {failure}",
                    role=step.role,
                    duration_ms=duration,
                )
                return self._finish("failed", error=f"{step.title}: {failure}")

        summary = f"{finished} of {total} steps finished"

        if skipped:
            summary += f", {skipped} skipped"

        # A task can put its own result (e.g. an answer) in ``state["summary"]``.
        return self._finish("completed", summary=self.state.get("summary") or summary)

    # -- helpers -----------------------------------------------------------

    def _ms_since(self, began: float) -> int:
        return int((self.clock() - began) * 1000)

    def _finish(
        self,
        status: str,
        *,
        summary: str | None = None,
        error: str | None = None,
    ) -> RunResult:
        seconds = self.elapsed_ms() / 1000
        label = {"completed": "completed", "failed": "failed", "cancelled": "cancelled"}[status]

        self.emit(
            "run_finished",
            f"Run {label} after {seconds:.1f}s",
            detail=error or summary,
        )

        return RunResult(status=status, summary=summary, error=error)
