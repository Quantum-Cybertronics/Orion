from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.backend.auth.dependencies import get_current_user
from app.backend.database import get_db
from app.backend.models import User
from app.backend.conversations.service import (
    ConversationNotFoundError,
    create_conversation,
    delete_conversation,
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

    return {"status": "deleted"}
