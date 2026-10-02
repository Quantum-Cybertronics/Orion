from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.backend.ai.providers import EchoAIProvider
from app.backend.ai.service import AIService


from app.backend.auth.session import SESSION_COOKIE_NAME, get_user_id
from app.backend.database import get_db
from app.backend.models import User
from app.backend.messages.service import (
    ConversationNotFoundError,
    create_message,
    create_message_with_assistant,
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

def get_ai_service() -> AIService:
    return AIService(EchoAIProvider())

@router.post("/")
def create(
    conversation_id: str,
    payload: CreateMessageRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_service: AIService = Depends(get_ai_service),
):
    try:
        if payload.role == "user":
            user_message, _ = create_message_with_assistant(
                db,
                user_id=user.id,
                conversation_id=conversation_id,
                role=payload.role,
                content=payload.content,
                ai_service=ai_service,
            )

            return {
                "id": user_message.id,
                "conversation_id": user_message.conversation_id,
                "role": user_message.role,
                "content": user_message.content,
                "created_at": user_message.created_at,
            }

        return create_message(
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
