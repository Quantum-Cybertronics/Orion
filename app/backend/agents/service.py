"""Database access for runs and their event logs."""

import json
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.backend.agents.events import Event
from app.backend.models import AgentRun, RunEvent

# "awaiting_approval": paused until the user decides about proposed changes.
ACTIVE_STATUSES = ("queued", "running", "awaiting_approval")

TITLE_LIMIT = 200
DETAIL_LIMIT = 20_000
EVENTS_PER_REQUEST = 1000

INTERRUPTED_MESSAGE = "ORION was restarted while this run was in progress."


class RunNotFoundError(Exception):
    pass


class RunAlreadyActiveError(Exception):
    """The user already has a run in progress."""


class RunStillActiveError(Exception):
    """A run that is still going cannot be deleted."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clip(text: str | None, limit: int) -> str | None:
    if text is None or len(text) <= limit:
        return text

    return text[: limit - 1] + "\u2026"


# -- used by the web requests -------------------------------------------------

def create_run(
    db: Session,
    user_id: str,
    kind: str,
    title: str,
    params: dict | None = None,
) -> AgentRun:
    """Record a new run. One run at a time per user (there is one AI model)."""
    active = db.scalar(
        select(AgentRun.id)
        .where(
            AgentRun.user_id == user_id,
            AgentRun.status.in_(ACTIVE_STATUSES),
        )
        .limit(1)
    )

    if active is not None:
        raise RunAlreadyActiveError

    run = AgentRun(
        user_id=user_id,
        kind=kind,
        title=_clip(title, TITLE_LIMIT),
        status="queued",
        params=json.dumps(params) if params else None,
    )

    db.add(run)
    db.commit()
    db.refresh(run)

    return run


def get_run(db: Session, user_id: str, run_id: str) -> AgentRun:
    run = db.get(AgentRun, run_id)

    if run is None or run.user_id != user_id:
        raise RunNotFoundError

    return run


def list_runs(db: Session, user_id: str, limit: int = 50) -> list[AgentRun]:
    statement = (
        select(AgentRun)
        .where(AgentRun.user_id == user_id)
        .order_by(AgentRun.created_at.desc())
        .limit(limit)
    )

    return list(db.scalars(statement).all())


def events_after(
    db: Session,
    run_id: str,
    after: int = 0,
    limit: int = EVENTS_PER_REQUEST,
) -> list[RunEvent]:
    """Events with ``seq`` greater than ``after``, oldest first."""
    statement = (
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.seq > after)
        .order_by(RunEvent.seq)
        .limit(limit)
    )

    return list(db.scalars(statement).all())


def delete_run(db: Session, user_id: str, run_id: str) -> None:
    run = get_run(db, user_id, run_id)

    if run.status in ACTIVE_STATUSES:
        raise RunStillActiveError

    db.execute(delete(RunEvent).where(RunEvent.run_id == run.id))
    db.delete(run)
    db.commit()


# -- used by the background worker (it opens its own short sessions) -----------

def mark_running(engine, run_id: str) -> None:
    with Session(engine) as session:
        session.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(status="running", started_at=_now())
        )
        session.commit()


def set_status(engine, run_id: str, status: str) -> None:
    """Change an active run's status (e.g. running <-> awaiting_approval).

    Never touches a finished run, so a late update cannot reopen it.
    """
    with Session(engine) as session:
        session.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status.in_(ACTIVE_STATUSES))
            .values(status=status)
        )
        session.commit()


def append_event(engine, run_id: str, event: Event) -> None:
    """Add an event to a run's log after the fact (e.g. when changes are undone)."""
    with Session(engine) as session:
        last_seq = session.scalar(
            select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)
        ) or 0
        last_elapsed = session.scalar(
            select(func.max(RunEvent.elapsed_ms)).where(RunEvent.run_id == run_id)
        ) or 0

        session.add(
            RunEvent(
                run_id=run_id,
                seq=last_seq + 1,
                type=event.type,
                role=event.role,
                title=_clip(event.title, TITLE_LIMIT),
                detail=_clip(event.detail, DETAIL_LIMIT),
                duration_ms=event.duration_ms,
                elapsed_ms=max(event.elapsed_ms, last_elapsed),   # never goes backwards
            )
        )
        session.commit()


def finish_run(
    engine,
    run_id: str,
    status: str,
    summary: str | None,
    error: str | None,
) -> None:
    """Close a run. Never touches a run that has already finished."""
    with Session(engine) as session:
        session.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status.in_(ACTIVE_STATUSES))
            .values(
                status=status,
                summary=_clip(summary, DETAIL_LIMIT),
                error=_clip(error, DETAIL_LIMIT),
                finished_at=_now(),
            )
        )
        session.commit()


class DbEventSink:
    """Writes each event to the database as it happens."""

    def __init__(self, engine, run_id: str) -> None:
        self._engine = engine
        self._run_id = run_id
        self._seq = 0

    def emit(self, event: Event) -> None:
        self._seq += 1

        with Session(self._engine) as session:
            session.add(
                RunEvent(
                    run_id=self._run_id,
                    seq=self._seq,
                    type=event.type,
                    role=event.role,
                    title=_clip(event.title, TITLE_LIMIT),
                    detail=_clip(event.detail, DETAIL_LIMIT),
                    duration_ms=event.duration_ms,
                    elapsed_ms=event.elapsed_ms,
                )
            )
            session.commit()


def fail_interrupted_runs(engine) -> int:
    """At startup: close runs a previous ORION process never finished.

    Their worker threads died with that process, so without this they would
    show as "running" forever (and block new runs).
    """
    with Session(engine) as session:
        run_ids = session.scalars(
            select(AgentRun.id).where(AgentRun.status.in_(ACTIVE_STATUSES))
        ).all()

        for run_id in run_ids:
            last_seq = session.scalar(
                select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)
            ) or 0
            last_elapsed = session.scalar(
                select(func.max(RunEvent.elapsed_ms)).where(RunEvent.run_id == run_id)
            ) or 0

            session.add(
                RunEvent(
                    run_id=run_id,
                    seq=last_seq + 1,
                    type="run_finished",
                    role="supervisor",
                    title="Run failed: ORION was restarted",
                    detail=INTERRUPTED_MESSAGE,
                    elapsed_ms=last_elapsed,
                )
            )

        if run_ids:
            session.execute(
                update(AgentRun)
                .where(AgentRun.id.in_(run_ids))
                .values(
                    status="failed",
                    error=INTERRUPTED_MESSAGE,
                    finished_at=_now(),
                )
            )

        session.commit()

        return len(run_ids)


# -- turning rows into JSON -----------------------------------------------------

def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)  # stored as UTC

    return value.isoformat()


def _params_of(run: AgentRun) -> dict:
    if not run.params:
        return {}

    try:
        value = json.loads(run.params)
    except ValueError:
        return {}

    return value if isinstance(value, dict) else {}


def run_to_dict(run: AgentRun) -> dict:
    return {
        "id": run.id,
        "kind": run.kind,
        "title": run.title,
        "params": _params_of(run),
        "status": run.status,
        "active": run.status in ACTIVE_STATUSES,
        "summary": run.summary,
        "error": run.error,
        "created_at": _iso(run.created_at),
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
    }


def event_to_dict(event: RunEvent) -> dict:
    return {
        "seq": event.seq,
        "type": event.type,
        "role": event.role,
        "title": event.title,
        "detail": event.detail,
        "duration_ms": event.duration_ms,
        "elapsed_ms": event.elapsed_ms,
    }
