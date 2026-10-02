from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.backend.auth.session import SESSION_COOKIE_NAME, get_user_id
from app.backend.database import get_db
from app.backend.models import User
from app.backend.messages.service import (
    ConversationNotFoundError,
    create_message,
    list_messages,
)


router = APIRouter(
    prefix="/conversations/{conversation_id}/messages",
    tags=["Messages"],
)


class CreateMessageRequest(BaseModel):
    role: str
    content: str


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    session_token = request.cookies.get(SESSION_COOKIE_NAME)

    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
        )

    user_id = get_user_id(session_token)

    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
        )

    user = db.get(User, user_id)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
        )

    return user


@router.post("/")
def create(
    conversation_id: str,
    payload: CreateMessageRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        message = create_message(
            db,
            user_id=user.id,
            conversation_id=conversation_id,
            role=payload.role,
            content=payload.content,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        ) from exc

    return message


@router.get("/")
def list_all(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return list_messages(
            db,
            user_id=user.id,
            conversation_id=conversation_id,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        ) from exc
