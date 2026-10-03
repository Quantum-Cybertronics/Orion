import uuid

from fastapi.testclient import TestClient
from app.backend.ai.service import AIService
from app.backend.main import app


client = TestClient(app)


def unique_username() -> str:
    return f"message_api_{uuid.uuid4().hex[:10]}"


def register_and_login(
    username: str,
    password: str = "TestPassword123!",
):
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


def create_conversation(title: str = "New conversation") -> str:
    response = client.post(
        "/conversations/",
        json={"title": title},
    )

    assert response.status_code == 200

    return response.json()["id"]


def test_messages_require_authentication():
    client.cookies.clear()

    conversation_id = str(uuid.uuid4())

    response = client.get(
        f"/conversations/{conversation_id}/messages/"
    )

    assert response.status_code == 401


def test_create_message():
    client.cookies.clear()

    register_and_login(unique_username())

    conversation_id = create_conversation()

    response = client.post(
        f"/conversations/{conversation_id}/messages/",
        json={
            "role": "user",
            "content": "Hello ORION",
        },
    )

    assert response.status_code == 200

    data = response.json()

    assert data["id"]
    assert data["conversation_id"] == conversation_id
    assert data["role"] == "user"
    assert data["content"] == "Hello ORION"

    print("CREATE MESSAGE RESPONSE:", response.status_code, response.json())


def test_list_messages():
    client.cookies.clear()

    register_and_login(unique_username())

    conversation_id = create_conversation()

    client.post(
        f"/conversations/{conversation_id}/messages/",
        json={
            "content": "Hello ORION",
        },
    )

    response = client.get(
        f"/conversations/{conversation_id}/messages/"
    )

    assert response.status_code == 200

    messages = response.json()

    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Hello ORION"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "ORION received: Hello ORION"


def test_user_cannot_create_message_in_another_users_conversation():
    client.cookies.clear()

    register_and_login(unique_username())

    conversation_id = create_conversation(
        "User A private conversation"
    )

    client.post("/auth/logout")

    register_and_login(unique_username())

    response = client.post(
        f"/conversations/{conversation_id}/messages/",
        json={
            "role": "user",
            "content": "Unauthorized message",
        },
    )

    assert response.status_code == 404


def test_user_cannot_read_another_users_messages():
    client.cookies.clear()

    register_and_login(unique_username())

    conversation_id = create_conversation(
        "User A private conversation"
    )

    client.post(
        f"/conversations/{conversation_id}/messages/",
        json={
            "role": "user",
            "content": "Private message",
        },
    )

    client.post("/auth/logout")

    register_and_login(unique_username())

    response = client.get(
        f"/conversations/{conversation_id}/messages/"
    )

    assert response.status_code == 404


def test_messages_are_isolated_between_conversations():
    client.cookies.clear()

    register_and_login(unique_username())

    conversation_a = create_conversation("Conversation A")
    conversation_b = create_conversation("Conversation B")

    client.post(
        f"/conversations/{conversation_a}/messages/",
        json={
            "role": "user",
            "content": "Message A",
        },
    )

    client.post(
        f"/conversations/{conversation_b}/messages/",
        json={
            "role": "user",
            "content": "Message B",
        },
    )

    response = client.get(
        f"/conversations/{conversation_a}/messages/"
    )

    assert response.status_code == 200

    messages = response.json()

    assert len(messages) == 2

    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Message A"

    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "ORION received: Message A"


def test_create_message_generates_assistant_response():
    client.cookies.clear()

    register_and_login(unique_username())

    conversation_id = create_conversation(
        "AI Test Conversation"
    )

    response = client.post(
        f"/conversations/{conversation_id}/messages/",
        json={
            "role": "user",
            "content": "Hello ORION",
        },
    )

    assert response.status_code == 200

    messages_response = client.get(
        f"/conversations/{conversation_id}/messages/"
    )

    assert messages_response.status_code == 200

    messages = messages_response.json()

    assert len(messages) == 2

    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Hello ORION"

    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "ORION received: Hello ORION"


def test_client_cannot_control_message_role():
    client.cookies.clear()

    register_and_login(unique_username())

    conversation_id = create_conversation()

    response = client.post(
        f"/conversations/{conversation_id}/messages/",
        json={
            "role": "assistant",
            "content": "I am pretending to be ORION.",
        },
    )

    assert response.status_code == 200

    messages_response = client.get(
        f"/conversations/{conversation_id}/messages/"
    )

    assert messages_response.status_code == 200

    messages = messages_response.json()

    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "I am pretending to be ORION."
    assert messages[1]["role"] == "assistant"


def test_ai_receives_conversation_history(monkeypatch):
    client.cookies.clear()

    received_histories = []

    original_generate_reply = AIService.generate_reply

    def capture_history(self, messages):
        received_histories.append(messages)
        return original_generate_reply(self, messages)

    monkeypatch.setattr(
        AIService,
        "generate_reply",
        capture_history,
    )

    register_and_login(unique_username())

    conversation_id = create_conversation()

    first_response = client.post(
        f"/conversations/{conversation_id}/messages/",
        json={
            "content": "My name is Alice.",
        },
    )

    assert first_response.status_code == 200

    second_response = client.post(
        f"/conversations/{conversation_id}/messages/",
        json={
            "content": "What is my name?",
        },
    )

    assert second_response.status_code == 200

    assert received_histories[0] == [
        {
            "role": "user",
            "content": "My name is Alice.",
        },
    ]

    assert received_histories[1] == [
        {
            "role": "user",
            "content": "My name is Alice.",
        },
        {
            "role": "assistant",
            "content": received_histories[1][1]["content"],
        },
        {
            "role": "user",
            "content": "What is my name?",
        },
    ]

    messages_response = client.get(
        f"/conversations/{conversation_id}/messages/"
    )

    assert messages_response.status_code == 200

    messages = messages_response.json()

    assert len(messages) == 4

    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "My name is Alice."

    assert messages[1]["role"] == "assistant"

    assert messages[2]["role"] == "user"
    assert messages[2]["content"] == "What is my name?"

    assert messages[3]["role"] == "assistant"


def test_first_user_message_generates_conversation_title():
    client.cookies.clear()

    register_and_login(unique_username())

    conversation_id = create_conversation()

    response = client.post(
        f"/conversations/{conversation_id}/messages/",
        json={
            "content": "How do I learn Python?",
        },
    )

    assert response.status_code == 200

    conversation_response = client.get(
        f"/conversations/{conversation_id}"
    )

    assert conversation_response.status_code == 200

    conversation = conversation_response.json()

    assert conversation["title"] == "How do I learn Python?"


def test_conversation_title_is_not_overwritten():
    client.cookies.clear()

    register_and_login(unique_username())

    conversation_id = create_conversation()

    first_response = client.post(
        f"/conversations/{conversation_id}/messages/",
        json={
            "content": "My first topic",
        },
    )

    assert first_response.status_code == 200

    second_response = client.post(
        f"/conversations/{conversation_id}/messages/",
        json={
            "content": "My completely different second topic",
        },
    )

    assert second_response.status_code == 200

    conversation_response = client.get(
        f"/conversations/{conversation_id}"
    )

    assert conversation_response.status_code == 200

    conversation = conversation_response.json()

    assert conversation["title"] == "My first topic"


def test_conversation_title_trims_whitespace():
    client.cookies.clear()

    register_and_login(unique_username())

    conversation_id = create_conversation()

    response = client.post(
        f"/conversations/{conversation_id}/messages/",
        json={
            "content": "   How do I learn Python?   ",
        },
    )

    assert response.status_code == 200

    conversation_response = client.get(
        f"/conversations/{conversation_id}"
    )

    assert conversation_response.status_code == 200

    conversation = conversation_response.json()

    assert conversation["title"] == "How do I learn Python?"


def test_conversation_title_collapses_whitespace():
    client.cookies.clear()

    register_and_login(unique_username())

    conversation_id = create_conversation()

    response = client.post(
        f"/conversations/{conversation_id}/messages/",
        json={
            "content": "How    do   I   learn   Python?",
        },
    )

    assert response.status_code == 200

    conversation_response = client.get(
        f"/conversations/{conversation_id}"
    )

    conversation = conversation_response.json()

    assert conversation["title"] == "How do I learn Python?"


def test_conversation_title_is_limited_to_80_characters():
    client.cookies.clear()

    register_and_login(unique_username())

    conversation_id = create_conversation()

    long_message = "A" * 200

    response = client.post(
        f"/conversations/{conversation_id}/messages/",
        json={
            "content": long_message,
        },
    )

    assert response.status_code == 200

    conversation_response = client.get(
        f"/conversations/{conversation_id}"
    )

    conversation = conversation_response.json()

    assert len(conversation["title"]) == 80
    assert conversation["title"] == "A" * 80