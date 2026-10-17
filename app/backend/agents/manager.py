"""Runs tasks on background threads and lets the user cancel them."""

import logging
import threading

from app.backend.agents.service import DbEventSink, finish_run, mark_running, set_status
from app.backend.agents.supervisor import ApprovalGate, Supervisor
from app.backend.agents.tasks import Task

logger = logging.getLogger("orion.agents")


class RunManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancels: dict[str, threading.Event] = {}
        self._gates: dict[str, ApprovalGate] = {}

    def start(self, engine, run_id: str, task: Task, params: dict | None = None) -> None:
        """Start the run on its own thread and return immediately."""
        cancel = threading.Event()
        gate = ApprovalGate()

        with self._lock:
            self._cancels[run_id] = cancel
            self._gates[run_id] = gate

        threading.Thread(
            target=self._execute,
            args=(engine, run_id, task, params or {}, cancel, gate),
            name=f"orion-run-{run_id[:8]}",
            daemon=True,  # never keeps ORION from exiting
        ).start()

    def request_cancel(self, run_id: str) -> bool:
        """Ask a running run to stop. False if no worker is running it."""
        with self._lock:
            cancel = self._cancels.get(run_id)

        if cancel is None:
            return False

        cancel.set()

        return True

    def decide(self, run_id: str, decision: str) -> bool:
        """Give a waiting run the user's decision. False if it is not waiting."""
        with self._lock:
            gate = self._gates.get(run_id)

        return gate is not None and gate.decide(decision)

    def cancel_all(self) -> None:
        """Ask every running run to stop (used when ORION shuts down)."""
        with self._lock:
            cancels = list(self._cancels.values())

        for cancel in cancels:
            cancel.set()

    def is_live(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._cancels

    def _execute(
        self,
        engine,
        run_id: str,
        task: Task,
        params: dict,
        cancel: threading.Event,
        gate: ApprovalGate,
    ) -> None:
        try:
            mark_running(engine, run_id)

            supervisor = Supervisor(
                DbEventSink(engine, run_id),
                cancel,
                task.limits,
                gate=gate,
                status_hook=lambda status: set_status(engine, run_id, status),
                state={"run_id": run_id},
            )
            result = supervisor.run(task.build(params), title=task.title_for(params))

            finish_run(engine, run_id, result.status, result.summary, result.error)
        except Exception:
            logger.exception("Run %s crashed", run_id)

            try:
                finish_run(engine, run_id, "failed", None, "The run stopped because of an internal error.")
            except Exception:
                logger.exception("Could not record the failure of run %s", run_id)
        finally:
            with self._lock:
                self._cancels.pop(run_id, None)
                self._gates.pop(run_id, None)


_manager: RunManager | None = None


def get_run_manager() -> RunManager:
    global _manager

    if _manager is None:
        _manager = RunManager()

    return _manager
