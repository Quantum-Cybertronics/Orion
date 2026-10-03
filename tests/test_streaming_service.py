import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.backend.ai.providers import EchoAIProvider
from app.backend.ai.service import AIService
from app.backend.conversations.service import create_conversation
from app.backend.database import Base
from app.backend.messages.service import (
    ConversationNotFoundError,
    begin_user_turn,
    list_messages,
    save_assistant_message,
)
from app.backend.models import User


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        yield session


def make_user(db) -> User:
    user = User(username=f"u_{uuid.uuid4().hex[:8]}", password_hash="x")
    db.add(user)
    db.commit()
    return user


def test_echo_stream_matches_generate():
    service = AIService(EchoAIProvider())
    messages = [{"role": "user", "content": "stream  me please"}]

    assert "".join(service.stream_reply(messages)) == service.generate_reply(messages)
    assert len(list(service.stream_reply(messages))) > 1


def test_stream_reply_rejects_empty_history():
    with pytest.raises(ValueError):
        AIService(EchoAIProvider()).stream_reply([])


def test_default_provider_stream_yields_whole_reply():
    from app.backend.ai.base import AIProvider

    class Plain(AIProvider):
        def generate(self, messages):
            return "all at once"

        def generate_title(self, content):
            return "t"

    assert list(Plain().stream([{"role": "user", "content": "x"}])) == ["all at once"]


def test_begin_user_turn_saves_message_and_returns_history(db):
    user = make_user(db)
    conversation = create_conversation(db, user_id=user.id)
    service = AIService(EchoAIProvider())

    first, history = begin_user_turn(db, user.id, conversation.id, "Hello", service)
    save_assistant_message(db, conversation.id, "Hi!")
    second, history2 = begin_user_turn(db, user.id, conversation.id, "Again", service)

    assert first.role == "user"
    assert history == [{"role": "user", "content": "Hello"}]
    assert history2 == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi!"},
        {"role": "user", "content": "Again"},
    ]
    assert conversation.title == "Hello"
    assert len(list_messages(db, user.id, conversation.id)) == 3


def test_begin_user_turn_enforces_ownership(db):
    owner = make_user(db)
    stranger = make_user(db)
    conversation = create_conversation(db, user_id=owner.id)

    with pytest.raises(ConversationNotFoundError):
        begin_user_turn(db, stranger.id, conversation.id, "hi", AIService(EchoAIProvider()))

    assert list_messages(db, owner.id, conversation.id) == []
