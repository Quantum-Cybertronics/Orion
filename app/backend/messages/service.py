from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.backend.ai.service import AIService
from app.backend.models import Conversation, Message


class ConversationNotFoundError(Exception):
    pass


MAX_CONVERSATION_TITLE_LENGTH = 80

# Insertion order. created_at alone can tie on platforms with a coarse clock
# (Windows ~15 ms), and UUID ids are random, so rowid is the stable tiebreaker.
MESSAGE_ORDER = (Message.created_at.asc(), text("messages.rowid"))


def fallback_conversation_title(content: str) -> str:
    title = " ".join(content.split())

    if len(title) > MAX_CONVERSATION_TITLE_LENGTH:
        title = title[:MAX_CONVERSATION_TITLE_LENGTH].rstrip()

    return title


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


def create_user_message(
    db: Session,
    user_id: str,
    conversation_id: str,
    content: str,
) -> Message:
    get_owned_conversation(
        db,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    message = Message(
        conversation_id=conversation_id,
        role="user",
        content=content,
    )

    db.add(message)
    db.commit()
    db.refresh(message)

    return message


def begin_user_turn(
    db: Session,
    user_id: str,
    conversation_id: str,
    content: str,
    ai_service: AIService,
) -> tuple[Message, list[dict[str, str]]]:
    """Save the user's message and return it with the conversation history.

    The history (oldest first, including the new message) is what gets sent
    to the AI. Sets the conversation title from the first message.
    """
    conversation = get_owned_conversation(
        db,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    is_first_message = len(conversation.messages) == 0

    user_message = Message(
        conversation_id=conversation.id,
        role="user",
        content=content,
    )

    db.add(user_message)

    if is_first_message and conversation.title == "New conversation":
        try:
            conversation.title = ai_service.generate_title(content)
        except Exception:
            conversation.title = fallback_conversation_title(content)

    db.commit()
    db.refresh(user_message)

    statement = (
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(*MESSAGE_ORDER)
    )

    history = [
        {
            "role": message.role,
            "content": message.content,
        }
        for message in db.scalars(statement).all()
    ]

    return user_message, history


def save_assistant_message(
    db: Session,
    conversation_id: str,
    content: str,
) -> Message:
    assistant_message = Message(
        conversation_id=conversation_id,
        role="assistant",
        content=content,
    )

    db.add(assistant_message)
    db.commit()
    db.refresh(assistant_message)

    return assistant_message


def create_message_with_assistant(
    db: Session,
    user_id: str,
    conversation_id: str,
    content: str,
    ai_service: AIService,
) -> tuple[Message, Message]:
    user_message, history = begin_user_turn(
        db,
        user_id=user_id,
        conversation_id=conversation_id,
        content=content,
        ai_service=ai_service,
    )

    assistant_content = ai_service.generate_reply(history)

    assistant_message = save_assistant_message(
        db,
        conversation_id=conversation_id,
        content=assistant_content,
    )

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
        .order_by(*MESSAGE_ORDER)
    )

    return list(db.scalars(statement).all())
