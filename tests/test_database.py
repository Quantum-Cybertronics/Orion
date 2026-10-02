import uuid

from sqlalchemy import select

from app.backend.models import Conversation, Message, User


def test_user_conversation_message_relationship(testing_session_local):
    db = testing_session_local()

    try:
        username = f"test_{uuid.uuid4().hex[:8]}"

        user = User(
            username=username,
            password_hash="temporary-test-hash",
        )

        db.add(user)
        db.flush()

        conversation = Conversation(
            user_id=user.id,
            title="Test conversation",
        )

        db.add(conversation)
        db.flush()

        message = Message(
            conversation_id=conversation.id,
            role="user",
            content="Hello ORION",
        )

        db.add(message)
        db.commit()

        saved_user = db.scalar(
            select(User).where(User.id == user.id)
        )

        assert saved_user is not None
        assert saved_user.username == username
        assert len(saved_user.conversations) == 1

        saved_conversation = saved_user.conversations[0]

        assert saved_conversation.title == "Test conversation"
        assert len(saved_conversation.messages) == 1
        assert saved_conversation.messages[0].content == "Hello ORION"

    finally:
        if "user" in locals() and user.id:
            db.delete(user)
            db.commit()

        db.close()
