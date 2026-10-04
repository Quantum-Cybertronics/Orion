from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.backend.auth.dependencies import get_current_user
from app.backend.database import get_db
from app.backend.models import User
from app.backend.storage import request_vacuum
from app.backend.conversations.service import (
    ConversationNotFoundError,
    create_conversation,
    delete_all_conversations,
    delete_conversation,
    delete_conversations_older_than,
    get_conversation,
    list_conversations,
)


router = APIRouter(prefix="/conversations", tags=["Conversations"])


class CreateConversationRequest(BaseModel):
    title: str | None = None


@router.post("/")
def create(
    payload: CreateConversationRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conversation = create_conversation(
        db,
        user_id=user.id,
        title=payload.title,
    )

    return conversation


@router.get("/")
def list_all(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return list_conversations(
        db,
        user_id=user.id,
    )


@router.get("/{conversation_id}")
def get_one(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return get_conversation(
            db,
            user_id=user.id,
            conversation_id=conversation_id,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        ) from exc


@router.delete("/{conversation_id}")
def delete(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        delete_conversation(
            db,
            user_id=user.id,
            conversation_id=conversation_id,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        ) from exc

    request_vacuum(db.get_bind())

    return {"status": "deleted"}


@router.delete("/")
def delete_many(
    older_than_days: int | None = Query(default=None, ge=1, le=3650),
    scope: str | None = Query(default=None, pattern="^all$"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Bulk delete the current user's conversations.

    Give exactly one of ``?older_than_days=N`` (no activity for N days) or
    ``?scope=all``. A bare DELETE is refused so it can never wipe everything
    by accident.
    """
    if (older_than_days is None) == (scope is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Give exactly one of older_than_days or scope=all.",
        )

    if scope == "all":
        deleted = delete_all_conversations(db, user_id=user.id)
    else:
        deleted = delete_conversations_older_than(
            db,
            user_id=user.id,
            days=older_than_days,
        )

    request_vacuum(db.get_bind())

    return {"deleted": deleted}
