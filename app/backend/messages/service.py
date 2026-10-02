from sqlalchemy import select
from sqlalchemy.orm import Session
from app.backend.ai.service import AIService

from app.backend.models import Conversation, Message


class ConversationNotFoundError(Exception):
    pass


def get_owned_conversation(
    db: Session,
    user_id: str,
    conversation_id: str,
) -> Conversation:
    statement = select(Conversation).where(
        Conversation.id == conversation_id,
        Conversation.user_id == user_id,
    )

    conversation = db.scalar(statement)

    if conversation is None:
        raise ConversationNotFoundError

    return conversation


def create_message(
    db: Session,
    user_id: str,
    conversation_id: str,
    role: str,
    content: str,
) -> Message:
    get_owned_conversation(
        db,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    message = Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
    )

    db.add(message)
    db.commit()
    db.refresh(message)

    return message


def create_message_with_assistant(
    db: Session,
    user_id: str,
    conversation_id: str,
    role: str,
    content: str,
    ai_service: AIService,
) -> tuple[Message, Message]:
    conversation = get_owned_conversation(
        db,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    user_message = Message(
        conversation_id=conversation.id,
        role=role,
        content=content,
    )

    db.add(user_message)
    db.commit()
    db.refresh(user_message)

    assistant_content = ai_service.generate_reply(
        [
            {
                "role": "user",
                "content": content,
            }
        ]
    )

    assistant_message = Message(
        conversation_id=conversation.id,
        role="assistant",
        content=assistant_content,
    )

    db.add(assistant_message)
    db.commit()
    db.refresh(assistant_message)

    return user_message, assistant_message





def list_messages(
    db: Session,
    user_id: str,
    conversation_id: str,
) -> list[Message]:
    get_owned_conversation(
        db,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    statement = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
    )

    return list(db.scalars(statement).all())
