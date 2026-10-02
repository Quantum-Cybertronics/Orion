import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.backend.conversations.service import (
    ConversationNotFoundError,
    create_conversation,
    delete_conversation,
    get_conversation,
    list_conversations,
)
from app.backend.database import Base
from app.backend.models import User



@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )

    Base.metadata.create_all(engine)

    with Session(engine) as session:
        yield session


def create_test_user(db: Session) -> User:
    user = User(
        id=str(uuid.uuid4()),
        username=f"user_{uuid.uuid4().hex[:8]}",
        password_hash="test-hash",
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    return user


def test_create_conversation(db):
    user = create_test_user(db)

    conversation = create_conversation(
        db,
        user_id=user.id,
        title="My first conversation",
    )

    assert conversation.id is not None
    assert conversation.user_id == user.id
    assert conversation.title == "My first conversation"


def test_list_conversations_only_returns_owned_conversations(db):
    user_a = create_test_user(db)
    user_b = create_test_user(db)

    create_conversation(
        db,
        user_id=user_a.id,
        title="A conversation",
    )

    create_conversation(
        db,
        user_id=user_b.id,
        title="B conversation",
    )

    conversations = list_conversations(
        db,
        user_id=user_a.id,
    )

    assert len(conversations) == 1
    assert conversations[0].user_id == user_a.id
    assert conversations[0].title == "A conversation"


def test_get_conversation_rejects_other_users(db):
    user_a = create_test_user(db)
    user_b = create_test_user(db)

    conversation = create_conversation(
        db,
        user_id=user_a.id,
        title="Private conversation",
    )

    with pytest.raises(ConversationNotFoundError):
        get_conversation(
            db,
            user_id=user_b.id,
            conversation_id=conversation.id,
        )


def test_get_conversation_returns_owned_conversation(db):
    user = create_test_user(db)

    conversation = create_conversation(
        db,
        user_id=user.id,
        title="Private conversation",
    )

    result = get_conversation(
        db,
        user_id=user.id,
        conversation_id=conversation.id,
    )

    assert result.id == conversation.id
    assert result.user_id == user.id


def test_delete_conversation_only_allows_owner(db):
    user_a = create_test_user(db)
    user_b = create_test_user(db)

    conversation = create_conversation(
        db,
        user_id=user_a.id,
        title="Protected conversation",
    )

    with pytest.raises(ConversationNotFoundError):
        delete_conversation(
            db,
            user_id=user_b.id,
            conversation_id=conversation.id,
        )

    assert (
        get_conversation(
            db,
            user_id=user_a.id,
            conversation_id=conversation.id,
        ).id
        == conversation.id
    )


def test_delete_conversation(db):
    user = create_test_user(db)

    conversation = create_conversation(
        db,
        user_id=user.id,
        title="Delete me",
    )

    delete_conversation(
        db,
        user_id=user.id,
        conversation_id=conversation.id,
    )

    with pytest.raises(ConversationNotFoundError):
        get_conversation(
            db,
            user_id=user.id,
            conversation_id=conversation.id,
        )
