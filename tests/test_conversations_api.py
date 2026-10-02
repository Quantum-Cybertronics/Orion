import uuid

from fastapi.testclient import TestClient

from app.backend.main import app


client = TestClient(app)


def unique_username() -> str:
    return f"conversation_api_{uuid.uuid4().hex[:10]}"


def register_and_login(username: str, password: str = "TestPassword123!"):
    register_response = client.post(
        "/auth/register",
        json={
            "username": username,
            "password": password,
        },
    )

    assert register_response.status_code == 201

    login_response = client.post(
        "/auth/login",
        json={
            "username": username,
            "password": password,
        },
    )

    assert login_response.status_code == 200


def test_conversations_require_authentication():
    client.cookies.clear()

    response = client.get("/conversations/")

    assert response.status_code == 401


def test_create_conversation():
    client.cookies.clear()

    register_and_login(unique_username())

    response = client.post(
        "/conversations/",
        json={
            "title": "My first conversation",
        },
    )

    assert response.status_code == 200

    data = response.json()

    assert data["id"]
    assert data["title"] == "My first conversation"
    assert data["user_id"]


def test_list_conversations_only_returns_current_users_conversations():
    client.cookies.clear()

    register_and_login(unique_username())

    create_response = client.post(
        "/conversations/",
        json={
            "title": "Private conversation",
        },
    )

    assert create_response.status_code == 200

    response = client.get("/conversations/")

    assert response.status_code == 200

    conversations = response.json()

    assert len(conversations) == 1
    assert conversations[0]["title"] == "Private conversation"


def test_user_cannot_access_another_users_conversation():
    client.cookies.clear()

    register_and_login(unique_username())

    create_response = client.post(
        "/conversations/",
        json={
            "title": "User A private conversation",
        },
    )

    assert create_response.status_code == 200

    conversation_id = create_response.json()["id"]

    client.post("/auth/logout")

    register_and_login(unique_username())

    response = client.get(
        f"/conversations/{conversation_id}"
    )

    assert response.status_code == 404


def test_user_cannot_delete_another_users_conversation():
    client.cookies.clear()

    register_and_login(unique_username())

    create_response = client.post(
        "/conversations/",
        json={
            "title": "Protected conversation",
        },
    )

    assert create_response.status_code == 200

    conversation_id = create_response.json()["id"]

    client.post("/auth/logout")

    register_and_login(unique_username())

    response = client.delete(
        f"/conversations/{conversation_id}"
    )

    assert response.status_code == 404


def test_owner_can_get_and_delete_conversation():
    client.cookies.clear()

    register_and_login(unique_username())

    create_response = client.post(
        "/conversations/",
        json={
            "title": "Conversation to delete",
        },
    )

    assert create_response.status_code == 200

    conversation_id = create_response.json()["id"]

    get_response = client.get(
        f"/conversations/{conversation_id}"
    )

    assert get_response.status_code == 200
    assert get_response.json()["id"] == conversation_id

    delete_response = client.delete(
        f"/conversations/{conversation_id}"
    )

    assert delete_response.status_code == 200
    assert delete_response.json() == {"status": "deleted"}

    get_after_delete = client.get(
        f"/conversations/{conversation_id}"
    )

    assert get_after_delete.status_code == 404
