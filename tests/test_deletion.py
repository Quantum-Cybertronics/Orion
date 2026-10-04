import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.backend import storage
from app.backend.ai.providers import EchoAIProvider
from app.backend.ai.service import AIService
from app.backend.conversations import service as conversations_service
from app.backend.conversations.service import (
    ConversationNotFoundError,
    create_conversation,
    delete_all_conversations,
    delete_conversation,
    delete_conversations_older_than,
    list_conversations,
)
from app.backend.database import Base
from app.backend.messages.service import (
    ConversationNotFoundError as MessageConversationNotFoundError,
    begin_user_turn,
    save_assistant_message,
)
from app.backend.models import Attachment, Conversation, Message, User


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


def make_conversation(db, user, *, messages=2, files=1, age_days=0) -> Conversation:
    conversation = create_conversation(db, user_id=user.id, title="chat")

    for index in range(messages):
        db.add(Message(conversation_id=conversation.id, role="user", content=f"m{index}"))

    for index in range(files):
        db.add(
            Attachment(
                conversation_id=conversation.id,
                filename=f"f{index}.txt",
                kind="text",
                size_bytes=5,
                text="hello",
                char_count=5,
                token_estimate=2,
            )
        )

    conversation.updated_at = datetime.now(timezone.utc) - timedelta(days=age_days)
    db.commit()

    return conversation


def count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


# ------------------------------------------------------------ single delete

def test_delete_removes_messages_and_attached_files_too(db):
    user = make_user(db)
    conversation = make_conversation(db, user, messages=3, files=2)

    delete_conversation(db, user.id, conversation.id)

    assert count(db, Conversation) == 0
    assert count(db, Message) == 0        # no orphaned messages
    assert count(db, Attachment) == 0     # uploaded text gone with it


def test_delete_leaves_other_conversations_alone(db):
    user = make_user(db)
    doomed = make_conversation(db, user, messages=2, files=1)
    kept = make_conversation(db, user, messages=4, files=2)

    delete_conversation(db, user.id, doomed.id)

    assert [c.id for c in list_conversations(db, user.id)] == [kept.id]
    assert count(db, Message) == 4
    assert count(db, Attachment) == 2


def test_cannot_delete_someone_elses_conversation(db):
    owner = make_user(db)
    stranger = make_user(db)
    conversation = make_conversation(db, owner)

    with pytest.raises(ConversationNotFoundError):
        delete_conversation(db, stranger.id, conversation.id)

    assert count(db, Conversation) == 1
    assert count(db, Message) == 2


def test_delete_unknown_conversation_raises(db):
    with pytest.raises(ConversationNotFoundError):
        delete_conversation(db, make_user(db).id, str(uuid.uuid4()))


# ---------------------------------------------------------------- bulk

def test_delete_all_only_touches_this_users_data(db):
    alice = make_user(db)
    bob = make_user(db)
    make_conversation(db, alice, messages=2, files=1)
    make_conversation(db, alice, messages=2, files=1)
    bobs = make_conversation(db, bob, messages=5, files=3)

    assert delete_all_conversations(db, alice.id) == 2

    assert [c.id for c in list_conversations(db, bob.id)] == [bobs.id]
    assert list_conversations(db, alice.id) == []
    assert count(db, Message) == 5
    assert count(db, Attachment) == 3


def test_delete_all_with_nothing_to_delete(db):
    assert delete_all_conversations(db, make_user(db).id) == 0


def test_older_than_uses_last_activity_and_keeps_recent(db):
    user = make_user(db)
    old = make_conversation(db, user, age_days=45)
    middle = make_conversation(db, user, age_days=10)
    new = make_conversation(db, user, age_days=0)
    old_id, middle_id, new_id = old.id, middle.id, new.id   # rows expire once deleted

    assert delete_conversations_older_than(db, user.id, days=30) == 1

    remaining = {c.id for c in list_conversations(db, user.id)}

    assert remaining == {middle_id, new_id}
    assert old_id not in remaining
    assert count(db, Attachment) == 2


def test_older_than_cutoff_is_exact_and_testable_with_now(db):
    user = make_user(db)
    now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    just_inside = make_conversation(db, user)
    just_outside = make_conversation(db, user)
    just_inside.updated_at = now - timedelta(days=30) + timedelta(minutes=1)
    just_outside.updated_at = now - timedelta(days=30) - timedelta(minutes=1)
    db.commit()

    assert delete_conversations_older_than(db, user.id, days=30, now=now) == 1
    assert [c.id for c in list_conversations(db, user.id)] == [just_inside.id]


def test_older_than_is_per_user(db):
    alice = make_user(db)
    bob = make_user(db)
    make_conversation(db, alice, age_days=90)
    bobs_old = make_conversation(db, bob, age_days=90)

    assert delete_conversations_older_than(db, alice.id, days=30) == 1
    assert [c.id for c in list_conversations(db, bob.id)] == [bobs_old.id]


def test_older_than_rejects_nonsense_days(db):
    with pytest.raises(ValueError):
        delete_conversations_older_than(db, make_user(db).id, days=0)


def test_large_deletes_are_chunked(db, monkeypatch):
    monkeypatch.setattr(conversations_service, "DELETE_CHUNK_SIZE", 3)
    user = make_user(db)

    for _ in range(10):
        make_conversation(db, user, messages=1, files=1)

    assert delete_all_conversations(db, user.id) == 10
    assert count(db, Conversation) == count(db, Message) == count(db, Attachment) == 0


def test_failed_bulk_delete_rolls_back_completely(db, monkeypatch):
    user = make_user(db)
    make_conversation(db, user)
    make_conversation(db, user)
    real_execute = db.execute

    def explode_on_conversations(statement, *args, **kwargs):
        if "DELETE FROM conversations" in str(statement):
            raise RuntimeError("disk full")

        return real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", explode_on_conversations)

    with pytest.raises(RuntimeError):
        delete_all_conversations(db, user.id)

    monkeypatch.undo()

    # children were deleted first, but the rollback restored them
    assert count(db, Conversation) == 2
    assert count(db, Message) == 4
    assert count(db, Attachment) == 2


# ----------------------------------------------------- activity timestamps

def test_sending_and_replying_refreshes_last_activity(db):
    user = make_user(db)
    conversation = make_conversation(db, user, messages=0, files=0, age_days=60)
    service = AIService(EchoAIProvider())

    begin_user_turn(db, user.id, conversation.id, "hello", service)
    db.refresh(conversation)
    after_send = conversation.updated_at

    assert datetime.now(timezone.utc).replace(tzinfo=None) - after_send.replace(tzinfo=None) < timedelta(minutes=1)

    conversation.updated_at = datetime.now(timezone.utc) - timedelta(days=60)
    db.commit()

    save_assistant_message(db, conversation.id, "hi there")
    db.refresh(conversation)

    assert datetime.now(timezone.utc).replace(tzinfo=None) - conversation.updated_at.replace(tzinfo=None) < timedelta(minutes=1)


def test_active_old_conversation_survives_older_than_cleanup(db):
    user = make_user(db)
    conversation = make_conversation(db, user, messages=1, files=0, age_days=90)

    begin_user_turn(db, user.id, conversation.id, "still talking", AIService(EchoAIProvider()))

    assert delete_conversations_older_than(db, user.id, days=30) == 0


def test_reply_for_a_deleted_conversation_is_not_saved(db):
    user = make_user(db)
    conversation = make_conversation(db, user)
    conversation_id = conversation.id

    delete_conversation(db, user.id, conversation_id)

    with pytest.raises(MessageConversationNotFoundError):
        save_assistant_message(db, conversation_id, "too late")

    assert count(db, Message) == 0   # no orphan row created


# ------------------------------------------------------------------ vacuum

@pytest.fixture
def file_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'big.db'}")
    Base.metadata.create_all(engine)

    yield engine, tmp_path / "big.db"

    engine.dispose()


def fill_and_delete(engine, megabytes: int):
    with Session(engine) as session:
        user = make_user(session)
        conversation = create_conversation(session, user_id=user.id)

        for index in range(megabytes):
            session.add(Message(conversation_id=conversation.id, role="user", content="x" * 1_000_000))

        session.commit()
        delete_all_conversations(session, user.id)


def test_deleting_rows_does_not_shrink_the_file_but_vacuum_does(file_engine):
    engine, path = file_engine

    fill_and_delete(engine, megabytes=3)
    size_after_delete = path.stat().st_size

    assert size_after_delete > 2_500_000                    # SQLite kept the pages
    assert storage.reclaimable_bytes(engine) > 2_500_000

    assert storage.vacuum_if_worthwhile(engine) is True

    assert path.stat().st_size < 200_000
    assert storage.reclaimable_bytes(engine) == 0


def test_vacuum_is_skipped_when_little_would_be_recovered(file_engine):
    engine, path = file_engine

    with Session(engine) as session:
        user = make_user(session)
        make_conversation(session, user)
        delete_all_conversations(session, user.id)

    assert storage.vacuum_if_worthwhile(engine) is False   # a few KB: not worth it


def test_vacuum_keeps_remaining_data_intact(file_engine):
    engine, _ = file_engine

    with Session(engine) as session:
        alice = make_user(session)
        keeper = make_conversation(session, alice, messages=3, files=1)
        keeper_id = keeper.id
        bob = make_user(session)
        create_conversation(session, user_id=bob.id)

        for _ in range(3):
            session.add(Message(conversation_id=keeper_id, role="assistant", content="y" * 1_000_000))

        session.commit()
        session.execute(text("DELETE FROM messages WHERE role = 'assistant'"))
        session.commit()

    assert storage.vacuum_if_worthwhile(engine) is True

    with Session(engine) as session:
        assert count(session, Message) == 3
        assert count(session, Attachment) == 1
        assert count(session, Conversation) == 2


def test_scheduler_runs_one_vacuum_for_many_requests(monkeypatch, file_engine):
    engine, _ = file_engine
    monkeypatch.setenv("ORION_AUTO_VACUUM", "1")
    calls = []
    done = threading.Event()

    def fake_vacuum(eng, *args, **kwargs):
        calls.append(eng)
        done.set()
        return True

    monkeypatch.setattr(storage, "vacuum_if_worthwhile", fake_vacuum)
    scheduler = storage.VacuumScheduler(delay=0.05)

    for _ in range(5):
        scheduler.request(engine)

    assert done.wait(2)
    time.sleep(0.15)

    assert len(calls) == 1

    scheduler.request(engine)           # a later deletion schedules a new one
    time.sleep(0.3)

    assert len(calls) == 2


def test_scheduler_does_nothing_when_disabled(monkeypatch, file_engine):
    engine, _ = file_engine
    monkeypatch.setenv("ORION_AUTO_VACUUM", "0")
    calls = []
    monkeypatch.setattr(storage, "vacuum_if_worthwhile", lambda *a, **k: calls.append(1))
    scheduler = storage.VacuumScheduler(delay=0.01)

    scheduler.request(engine)
    time.sleep(0.1)

    assert calls == []


def test_scheduler_survives_a_failing_vacuum(monkeypatch, file_engine):
    engine, _ = file_engine
    monkeypatch.setenv("ORION_AUTO_VACUUM", "1")
    attempts = []

    def locked(*args, **kwargs):
        attempts.append(1)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(storage, "vacuum_if_worthwhile", locked)
    scheduler = storage.VacuumScheduler(delay=0.02)

    scheduler.request(engine)
    time.sleep(0.2)
    scheduler.request(engine)           # not stuck: can schedule again afterwards
    time.sleep(0.2)

    assert len(attempts) == 2


def test_compact_on_shutdown_cancels_pending_and_swallows_errors(monkeypatch, file_engine):
    engine, _ = file_engine
    monkeypatch.setenv("ORION_AUTO_VACUUM", "1")
    monkeypatch.setattr(storage, "vacuum_if_worthwhile", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    storage.scheduler.request(engine)
    storage.compact_on_shutdown(engine)      # must not raise

    assert storage.scheduler._timer is None
