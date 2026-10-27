from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.backend.agents.changes import (
    ChangeError,
    changes_summary,
    discard_backups,
    undo_changes,
)
from app.backend.agents.events import Event
from app.backend.agents.manager import get_run_manager
from app.backend.agents.service import (
    ACTIVE_STATUSES,
    append_event,
    RunAlreadyActiveError,
    RunNotFoundError,
    RunStillActiveError,
    create_run,
    delete_run,
    event_to_dict,
    events_after,
    finish_run,
    get_run,
    list_runs,
    run_to_dict,
)
from app.backend.agents.supervisor import AcknowledgementRequired
from app.backend.agents.tasks import ParamError, backup_dir_of, get_task, list_tasks
from app.backend.auth.dependencies import get_current_user
from app.backend.config import DATA_DIR
from app.backend.database import get_db
from app.backend.models import User

router = APIRouter(
    prefix="/runs",
    tags=["Runs"],
)


class CreateRunRequest(BaseModel):
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)


class DecisionRequest(BaseModel):
    decision: Literal["apply", "reject"]
    # Must be true to apply a change the security checks rated high-risk.
    acknowledge_risks: bool = False


def present(run) -> dict:
    """A run for the page: its data plus what could be undone, if anything."""
    data = run_to_dict(run)
    changes = changes_summary(backup_dir_of(run.id))

    if changes:
        data["changes"] = changes

    return data


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Run not found.",
    )


@router.get("/kinds")
def kinds(user: User = Depends(get_current_user)):
    """The kinds of run that can be started. Declared before ``/{run_id}``."""
    return [
        {
            "kind": task.kind,
            "title": task.title,
            "description": task.description,
            "fields": [
                {
                    "name": f.name,
                    "label": f.label,
                    "kind": f.kind,
                    "placeholder": f.placeholder,
                }
                for f in task.fields
            ],
        }
        for task in list_tasks()
    ]


@router.post("/", status_code=status.HTTP_201_CREATED)
def create(
    payload: CreateRunRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task = get_task(payload.kind)

    if task is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unknown kind of run.",
        )

    try:
        params = task.validate(payload.params)
    except ParamError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    try:
        run = create_run(
            db,
            user_id=user.id,
            kind=task.kind,
            title=task.title_for(params),
            params=params,
        )
    except RunAlreadyActiveError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A run is already in progress. Wait for it to finish or cancel it.",
        ) from exc

    data = present(run)

    # The worker opens its own sessions on this same database.
    get_run_manager().start(db.get_bind(), run.id, task, params)

    return data


@router.get("/")
def list_all(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return [present(run) for run in list_runs(db, user_id=user.id)]


@router.get("/{run_id}")
def get_one(
    run_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return present(get_run(db, user_id=user.id, run_id=run_id))
    except RunNotFoundError as exc:
        raise _not_found() from exc


@router.get("/{run_id}/events")
def get_events(
    run_id: str,
    after: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Events newer than ``after`` plus the run's current state.

    The page asks again with the last ``seq`` it has until the run is no
    longer active, so each call returns only what is new.
    """
    try:
        run = get_run(db, user_id=user.id, run_id=run_id)
    except RunNotFoundError as exc:
        raise _not_found() from exc

    return {
        "run": present(run),
        "events": [event_to_dict(event) for event in events_after(db, run.id, after)],
    }


@router.post("/{run_id}/cancel")
def cancel(
    run_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        run = get_run(db, user_id=user.id, run_id=run_id)
    except RunNotFoundError as exc:
        raise _not_found() from exc

    if run.status not in ACTIVE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This run has already finished.",
        )

    if not get_run_manager().request_cancel(run.id):
        # Nothing is running it (e.g. left over from a crash): close it here.
        finish_run(db.get_bind(), run.id, "cancelled", None, None)

    db.refresh(run)

    return present(run)


@router.post("/{run_id}/decision")
def decide(
    run_id: str,
    payload: DecisionRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Approve or reject proposed changes the run is waiting on."""
    try:
        run = get_run(db, user_id=user.id, run_id=run_id)
    except RunNotFoundError as exc:
        raise _not_found() from exc

    try:
        accepted = run.status == "awaiting_approval" and get_run_manager().decide(
            run.id, payload.decision, payload.acknowledge_risks
        )
    except AcknowledgementRequired as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "The security checks found high-risk problems with this change. "
                "Review them, then confirm that you want to apply it anyway."
            ),
        ) from exc

    if not accepted:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This run is not waiting for a decision.",
        )

    db.refresh(run)

    return present(run)


@router.post("/{run_id}/undo")
def undo(
    run_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Put back the files an approved run changed (only where they are still ORION's)."""
    try:
        run = get_run(db, user_id=user.id, run_id=run_id)
    except RunNotFoundError as exc:
        raise _not_found() from exc

    if run.status in ACTIVE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Wait for the run to finish before undoing its changes.",
        )

    try:
        report = undo_changes(backup_dir_of(run.id), blocked=[DATA_DIR])
    except ChangeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    parts = []

    if report.restored:
        parts.append(f"restored {len(report.restored)}")

    if report.deleted:
        parts.append(f"removed {len(report.deleted)} new")

    if report.skipped:
        parts.append(f"left {len(report.skipped)} alone")

    detail = "\n".join(
        [f"Restored: {name}" for name in report.restored]
        + [f"Removed: {name}" for name in report.deleted]
        + [f"Left alone: {name} ({why})" for name, why in report.skipped]
    )

    append_event(
        db.get_bind(),
        run.id,
        Event("info", "Undo: " + (", ".join(parts) or "nothing to change"), role="supervisor", detail=detail),
    )

    db.refresh(run)

    return {
        "run": present(run),
        "undo": {
            "restored": report.restored,
            "deleted": report.deleted,
            "skipped": [{"path": name, "reason": why} for name, why in report.skipped],
        },
    }


@router.delete("/{run_id}")
def delete(
    run_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        delete_run(db, user_id=user.id, run_id=run_id)
    except RunNotFoundError as exc:
        raise _not_found() from exc
    except RunStillActiveError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cancel the run before deleting it.",
        ) from exc

    discard_backups(backup_dir_of(run_id))   # deleting a run also forgets how to undo it

    return {"deleted": True}
