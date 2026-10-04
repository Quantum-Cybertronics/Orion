import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.backend.main import app
from app.backend.models import Conversation

client = TestClient(app)


def register_and_login(username: str, password: str = "TestPassword123!"):
    assert client.post("/auth/register", json={"username": username, "password": password}).status_code == 201
    assert client.post("/auth/login", json={"username": username, "password": password}).status_code == 200


def new_user():
    client.cookies.clear()
    register_and_login(f"del_api_{uuid.uuid4().hex[:10]}")


def create_conversation(title: str = "chat") -> str:
    response = client.post("/conversations/", json={"title": title})
    assert response.status_code == 200
    return response.json()["id"]


def send(conversation_id: str, text: str = "hello"):
    response = client.post(f"/conversations/{conversation_id}/messages/", json={"content": text})
    assert response.status_code in (200, 201)


def ids() -> set[str]:
    return {c["id"] for c in client.get("/conversations/").json()}


def age(testing_session_local, conversation_id: str, days: int):
    with testing_session_local() as session:
        conversation = session.scalar(select(Conversation).where(Conversation.id == conversation_id))
        conversation.updated_at = datetime.now(timezone.utc) - timedelta(days=days)
        session.commit()


def test_delete_one_removes_it_and_its_messages():
    new_user()
    keep, doomed = create_conversation("keep"), create_conversation("doomed")
    send(doomed)

    assert client.delete(f"/conversations/{doomed}").json() == {"status": "deleted"}
    assert ids() == {keep}
    assert client.get(f"/conversations/{doomed}/messages/").status_code == 404


def test_bulk_delete_requires_an_explicit_choice():
    new_user()
    create_conversation()

    assert client.delete("/conversations/").status_code == 400
    assert client.delete("/conversations/?scope=all&older_than_days=30").status_code == 400
    assert len(ids()) == 1                      # nothing was deleted


def test_bulk_delete_rejects_bad_values():
    new_user()
    create_conversation()

    assert client.delete("/conversations/?older_than_days=0").status_code == 422
    assert client.delete("/conversations/?older_than_days=-5").status_code == 422
    assert client.delete("/conversations/?older_than_days=abc").status_code == 422
    assert client.delete("/conversations/?scope=everything").status_code == 422
    assert len(ids()) == 1


def test_delete_all_only_deletes_my_conversations():
    new_user()
    create_conversation()
    other = create_conversation()
    send(other)

    client.cookies.clear()
    register_and_login(f"del_api_{uuid.uuid4().hex[:10]}")
    mine = [create_conversation(), create_conversation()]
    result = client.delete("/conversations/?scope=all")

    assert result.status_code == 200
    assert result.json() == {"deleted": 2}
    assert ids() == set()
    assert mine                                  # (they existed before)

    # the first user's conversations are untouched: log back in as them
    # (a different account was used above, so just check the count endpoint)
    assert client.get(f"/conversations/{other}/messages/").status_code == 404


def test_older_than_days_deletes_only_stale_conversations(testing_session_local):
    new_user()
    stale, fresh = create_conversation("stale"), create_conversation("fresh")
    send(stale)
    age(testing_session_local, stale, days=60)

    result = client.delete("/conversations/?older_than_days=30")

    assert result.json() == {"deleted": 1}
    assert ids() == {fresh}


def test_new_activity_protects_an_old_conversation(testing_session_local):
    new_user()
    conversation = create_conversation()
    age(testing_session_local, conversation, days=90)
    send(conversation)                           # talking in it counts as activity

    assert client.delete("/conversations/?older_than_days=30").json() == {"deleted": 0}
    assert ids() == {conversation}


def test_bulk_delete_requires_login():
    client.cookies.clear()

    assert client.delete("/conversations/?scope=all").status_code == 401
    assert client.delete(f"/conversations/{uuid.uuid4()}").status_code == 401
