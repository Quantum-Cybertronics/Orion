import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.backend.conversations.service import create_conversation
from app.backend.database import Base
from app.backend.messages.service import (
    ConversationNotFoundError,
    create_message,
    list_messages,
)
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


def test_create_message(db):
    user = create_test_user(db)

    conversation = create_conversation(
        db,
        user_id=user.id,
        title="Test conversation",
    )

    message = create_message(
        db,
        user_id=user.id,
        conversation_id=conversation.id,
        role="user",
        content="Hello ORION",
    )

    assert message.id is not None
    assert message.conversation_id == conversation.id
    assert message.role == "user"
    assert message.content == "Hello ORION"


def test_list_messages_returns_messages_in_creation_order(db):
    user = create_test_user(db)

    conversation = create_conversation(
        db,
        user_id=user.id,
        title="Test conversation",
    )

    first = create_message(
        db,
        user_id=user.id,
        conversation_id=conversation.id,
        role="user",
        content="First message",
    )

    second = create_message(
        db,
        user_id=user.id,
        conversation_id=conversation.id,
        role="assistant",
        content="Second message",
    )

    messages = list_messages(
        db,
        user_id=user.id,
        conversation_id=conversation.id,
    )

    assert [message.id for message in messages] == [
        first.id,
        second.id,
    ]


def test_user_cannot_create_message_in_another_users_conversation(db):
    user_a = create_test_user(db)
    user_b = create_test_user(db)

    conversation = create_conversation(
        db,
        user_id=user_a.id,
        title="Private conversation",
    )

    with pytest.raises(ConversationNotFoundError):
        create_message(
            db,
            user_id=user_b.id,
            conversation_id=conversation.id,
            role="user",
            content="Unauthorized message",
        )


def test_user_cannot_list_another_users_messages(db):
    user_a = create_test_user(db)
    user_b = create_test_user(db)

    conversation = create_conversation(
        db,
        user_id=user_a.id,
        title="Private conversation",
    )

    create_message(
        db,
        user_id=user_a.id,
        conversation_id=conversation.id,
        role="user",
        content="Private message",
    )

    with pytest.raises(ConversationNotFoundError):
        list_messages(
            db,
            user_id=user_b.id,
            conversation_id=conversation.id,
        )


def test_list_messages_only_returns_messages_for_requested_conversation(db):
    user = create_test_user(db)

    conversation_a = create_conversation(
        db,
        user_id=user.id,
        title="Conversation A",
    )

    conversation_b = create_conversation(
        db,
        user_id=user.id,
        title="Conversation B",
    )

    create_message(
        db,
        user_id=user.id,
        conversation_id=conversation_a.id,
        role="user",
        content="Message A",
    )

    create_message(
        db,
        user_id=user.id,
        conversation_id=conversation_b.id,
        role="user",
        content="Message B",
    )

    messages = list_messages(
        db,
        user_id=user.id,
        conversation_id=conversation_a.id,
    )

    assert len(messages) == 1
    assert messages[0].content == "Message A"
