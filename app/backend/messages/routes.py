import json
import logging
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.backend.ai.base import AIProviderError
from app.backend.ai.runtime import get_ai_service as get_runtime_ai_service
from app.backend.ai.service import AIService


from app.backend.auth.dependencies import get_current_user
from app.backend.database import get_db
from app.backend.models import User
from app.backend.messages.service import (
    ConversationNotFoundError,
    begin_user_turn,
    create_user_message,
    create_message_with_assistant,
    list_messages,
    save_assistant_message,
)

logger = logging.getLogger("orion.messages")


router = APIRouter(
    prefix="/conversations/{conversation_id}/messages",
    tags=["Messages"],
)


class CreateMessageRequest(BaseModel):
    content: str


def get_ai_service() -> AIService:
    return get_runtime_ai_service()


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

@router.post("/")
def create(
    conversation_id: str,
    payload: CreateMessageRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_service: AIService = Depends(get_ai_service),
):
    try:
        user_message, _ = create_message_with_assistant(
            db,
            user_id=user.id,
            conversation_id=conversation_id,
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

    except ConversationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        ) from exc
    except AIProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.post("/stream")
def create_streaming(
    conversation_id: str,
    payload: CreateMessageRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    ai_service: AIService = Depends(get_ai_service),
):
    """Save the user's message, then stream the reply as server-sent events.

    Events (one JSON object per ``data:`` line):
      user_message  the saved user message
      delta         a piece of the reply text
      done          the reply finished and was saved
      error         generation failed; any partial reply is kept
    """
    try:
        user_message, history = begin_user_turn(
            db,
            user_id=user.id,
            conversation_id=conversation_id,
            content=payload.content,
            ai_service=ai_service,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found.",
        ) from exc

    # The generator outlives this request's dependencies, so it opens its own
    # short-lived sessions on the same database instead of reusing `db`.
    engine = db.get_bind()

    user_event = {
        "type": "user_message",
        "id": user_message.id,
        "conversation_id": user_message.conversation_id,
        "role": user_message.role,
        "content": user_message.content,
        "created_at": user_message.created_at.isoformat(),
    }

    def save_reply(parts: list[str]):
        content = "".join(parts)

        if not content.strip():
            return None

        try:
            with Session(engine) as session:
                return save_assistant_message(
                    session,
                    conversation_id=conversation_id,
                    content=content,
                )
        except ConversationNotFoundError:
            return None  # conversation was deleted while replying

    def event_stream() -> Iterator[str]:
        parts: list[str] = []

        yield _sse(user_event)

        try:
            for chunk in ai_service.stream_reply(history):
                parts.append(chunk)
                yield _sse({"type": "delta", "content": chunk})
        except GeneratorExit:
            save_reply(parts)  # client went away; keep what was generated
            raise
        except AIProviderError as exc:
            save_reply(parts)
            yield _sse({"type": "error", "detail": str(exc)})
            return
        except Exception:
            logger.exception("Unexpected error while streaming a reply")
            save_reply(parts)
            yield _sse({"type": "error", "detail": "Something went wrong generating the reply."})
            return

        saved = save_reply(parts)

        yield _sse(
            {
                "type": "done",
                "id": saved.id if saved else None,
                "created_at": saved.created_at.isoformat() if saved else None,
            }
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


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
