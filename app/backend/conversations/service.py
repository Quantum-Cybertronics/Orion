from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backend.models import Conversation


class ConversationNotFoundError(Exception):
    pass


def create_conversation(
    db: Session,
    user_id: str,
    title: str | None = None,
) -> Conversation:
    conversation = Conversation(
        user_id=user_id,
        title=title or "New conversation",
    )

    db.add(conversation)
    db.commit()
    db.refresh(conversation)

    return conversation


def list_conversations(
    db: Session,
    user_id: str,
) -> list[Conversation]:
    statement = (
        select(Conversation)
        .where(Conversation.user_id == user_id)
        .order_by(Conversation.created_at.desc())
    )

    return list(db.scalars(statement).all())


def get_conversation(
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


def delete_conversation(
    db: Session,
    user_id: str,
    conversation_id: str,
) -> None:
    conversation = get_conversation(
        db,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    db.delete(conversation)
    db.commit()

# def generate_conversation_title(content: str) -> str:
#     title = " ".join(content.strip().split())
#
#     if not title:
#         return "New conversation"
#
#     if len(title) > 60:
#         title = title[:57].rstrip() + "..."
#
#     return title
