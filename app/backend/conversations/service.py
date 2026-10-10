from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.backend.conversations.formatting import (
    LIKE_ESCAPE,
    clean_title,
    like_pattern,
    make_snippet,
    normalize_query,
)
from app.backend.messages.service import MESSAGE_ORDER
from app.backend.models import Attachment, Conversation, Message

# SQLite limits how many values one IN (...) may hold; stay well under it.
DELETE_CHUNK_SIZE = 500


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


def rename_conversation(
    db: Session,
    user_id: str,
    conversation_id: str,
    title: str,
) -> Conversation:
    """Give a conversation a new title. ``ValueError`` if the title is unusable."""
    cleaned = clean_title(title)

    conversation = get_conversation(
        db,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    conversation.title = cleaned

    db.commit()
    db.refresh(conversation)

    return conversation


def list_conversation_messages(
    db: Session,
    conversation_id: str,
) -> list[Message]:
    """Messages oldest first. The caller must already have checked ownership."""
    statement = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(*MESSAGE_ORDER)
    )

    return list(db.scalars(statement).all())


SEARCH_RESULT_LIMIT = 30
SEARCH_ROW_LIMIT = 2000  # safety cap on matching message rows scanned


def search_conversations(
    db: Session,
    user_id: str,
    query: str,
    limit: int = SEARCH_RESULT_LIMIT,
) -> list[dict]:
    """Find this user's conversations by title or message text.

    Returns one entry per conversation (most recently active first) with a
    short ``snippet`` of the first matching message, or ``None`` when only
    the title matched. The search is a case-insensitive substring match.
    """
    needle = normalize_query(query)

    if not needle:
        return []

    found: dict[str, dict] = {}
    pattern = like_pattern(needle)

    title_matches = db.scalars(
        select(Conversation)
        .where(
            Conversation.user_id == user_id,
            Conversation.title.ilike(pattern, escape=LIKE_ESCAPE),
        )
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
    )

    for conversation in title_matches:
        found[conversation.id] = {
            "id": conversation.id,
            "title": conversation.title,
            "updated_at": conversation.updated_at,
            "snippet": None,
        }

    message_matches = db.execute(
        select(Conversation, Message)
        .join(Message, Message.conversation_id == Conversation.id)
        .where(
            Conversation.user_id == user_id,
            Message.content.ilike(pattern, escape=LIKE_ESCAPE),
        )
        .order_by(Conversation.updated_at.desc(), Message.created_at.desc())
        .limit(SEARCH_ROW_LIMIT)
    )

    for conversation, message in message_matches:
        entry = found.get(conversation.id)

        if entry is None:
            entry = found[conversation.id] = {
                "id": conversation.id,
                "title": conversation.title,
                "updated_at": conversation.updated_at,
                "snippet": None,
            }

        if entry["snippet"] is None:
            entry["snippet"] = make_snippet(message.content, needle)

    results = sorted(found.values(), key=lambda item: item["updated_at"], reverse=True)

    return results[:limit]


def _delete_conversations(db: Session, conversation_ids: list[str]) -> int:
    """Delete conversations together with their messages and attached files.

    Explicit deletes (children first) rather than relying on database-level
    cascades, which SQLite only applies when foreign keys are switched on.
    All in one transaction: either everything goes or nothing does.
    """
    try:
        for start in range(0, len(conversation_ids), DELETE_CHUNK_SIZE):
            chunk = conversation_ids[start:start + DELETE_CHUNK_SIZE]

            db.execute(delete(Attachment).where(Attachment.conversation_id.in_(chunk)))
            db.execute(delete(Message).where(Message.conversation_id.in_(chunk)))
            db.execute(delete(Conversation).where(Conversation.id.in_(chunk)))

        db.commit()
    except Exception:
        db.rollback()
        raise

    return len(conversation_ids)


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

    _delete_conversations(db, [conversation.id])


def delete_all_conversations(db: Session, user_id: str) -> int:
    """Delete every conversation belonging to this user. Returns the count."""
    ids = list(
        db.scalars(select(Conversation.id).where(Conversation.user_id == user_id))
    )

    return _delete_conversations(db, ids)


def delete_conversations_older_than(
    db: Session,
    user_id: str,
    days: int,
    now: datetime | None = None,
) -> int:
    """Delete this user's conversations with no activity for ``days`` days."""
    if days < 1:
        raise ValueError("days must be at least 1")

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)

    ids = list(
        db.scalars(
            select(Conversation.id).where(
                Conversation.user_id == user_id,
                Conversation.updated_at < cutoff,
            )
        )
    )

    return _delete_conversations(db, ids)

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
