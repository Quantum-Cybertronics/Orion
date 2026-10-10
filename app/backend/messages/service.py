from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.backend.ai.service import AIService
from app.backend.attachments.context import build_file_context, load_attachments
from app.backend.models import Conversation, Message


class ConversationNotFoundError(Exception):
    pass


class NothingToRegenerateError(Exception):
    """The conversation has no user message to answer."""


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


def _build_history(
    db: Session,
    conversation_id: str,
    messages: list[Message],
) -> list[dict[str, str]]:
    """What gets sent to the AI: the messages, plus any attached files."""
    history = [
        {
            "role": message.role,
            "content": message.content,
        }
        for message in messages
    ]

    # Attached files ride along as a system message on every turn. They are
    # not stored as chat messages, so they never appear in the transcript.
    file_context = build_file_context(load_attachments(db, conversation_id))

    if file_context:
        history.insert(0, {"role": "system", "content": file_context})

    return history


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

    conversation.updated_at = datetime.now(timezone.utc)  # last activity

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

    history = _build_history(db, conversation.id, list(db.scalars(statement).all()))

    return user_message, history


def begin_regeneration(
    db: Session,
    user_id: str,
    conversation_id: str,
) -> tuple[list[dict[str, str]], str | None]:
    """Prepare to answer the last user message again.

    Returns the history to send (ending with that user message) and the id
    of the assistant reply it will replace, or ``None`` if the last message
    is the user's own (its reply never got saved). Nothing is deleted here:
    the old reply is only removed once the new one is saved, so a failed
    regeneration never costs the user their existing answer.
    """
    conversation = get_owned_conversation(
        db,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    statement = (
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(*MESSAGE_ORDER)
    )

    messages = list(db.scalars(statement).all())
    replaces: str | None = None

    if messages and messages[-1].role == "assistant":
        replaces = messages[-1].id
        messages = messages[:-1]

    if not messages or messages[-1].role != "user":
        raise NothingToRegenerateError

    return _build_history(db, conversation.id, messages), replaces


def save_assistant_message(
    db: Session,
    conversation_id: str,
    content: str,
    replace_message_id: str | None = None,
) -> Message:
    """Save a reply. With ``replace_message_id``, swap it for that old reply.

    The swap is one transaction: the new reply exists and the old one is gone,
    or neither change happened.
    """
    conversation = db.get(Conversation, conversation_id)

    if conversation is None:
        # Deleted while the reply was being written (e.g. from another tab).
        raise ConversationNotFoundError

    assistant_message = Message(
        conversation_id=conversation_id,
        role="assistant",
        content=content,
    )

    conversation.updated_at = datetime.now(timezone.utc)  # last activity

    db.add(assistant_message)

    if replace_message_id:
        old = db.get(Message, replace_message_id)

        # Only ever replaces an assistant reply in this same conversation.
        if (
            old is not None
            and old.conversation_id == conversation_id
            and old.role == "assistant"
        ):
            db.delete(old)

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
