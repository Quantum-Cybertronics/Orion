import json
import uuid

from fastapi.testclient import TestClient

from app.backend.main import app

client = TestClient(app)


def unique_username() -> str:
    return f"stream_api_{uuid.uuid4().hex[:10]}"


def register_and_login(username: str, password: str = "TestPassword123!"):
    assert client.post(
        "/auth/register", json={"username": username, "password": password}
    ).status_code == 201
    assert client.post(
        "/auth/login", json={"username": username, "password": password}
    ).status_code == 200


def create_conversation() -> str:
    response = client.post("/conversations/", json={})
    assert response.status_code == 200
    return response.json()["id"]


def read_events(response) -> list[dict]:
    events = []

    for block in response.text.split("\n\n"):
        block = block.strip()

        if block.startswith("data:"):
            events.append(json.loads(block[len("data:"):].strip()))

    return events


def test_stream_requires_authentication():
    client.cookies.clear()

    response = client.post(
        f"/conversations/{uuid.uuid4()}/messages/stream",
        json={"content": "hi"},
    )

    assert response.status_code == 401


def test_stream_unknown_conversation_is_404():
    client.cookies.clear()
    register_and_login(unique_username())

    response = client.post(
        f"/conversations/{uuid.uuid4()}/messages/stream",
        json={"content": "hi"},
    )

    assert response.status_code == 404


def test_stream_emits_events_and_saves_reply():
    client.cookies.clear()
    register_and_login(unique_username())
    conversation_id = create_conversation()

    response = client.post(
        f"/conversations/{conversation_id}/messages/stream",
        json={"content": "Hello ORION streaming"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = read_events(response)
    kinds = [event["type"] for event in events]

    assert kinds[0] == "user_message"
    assert kinds[-1] == "done"
    assert kinds.count("delta") > 1

    streamed = "".join(e["content"] for e in events if e["type"] == "delta")
    assert streamed == "ORION received: Hello ORION streaming"

    saved = client.get(f"/conversations/{conversation_id}/messages/").json()

    assert [m["role"] for m in saved] == ["user", "assistant"]
    assert saved[1]["content"] == streamed
    assert events[-1]["id"] == saved[1]["id"]


def test_stream_cannot_use_another_users_conversation():
    client.cookies.clear()
    register_and_login(unique_username())
    conversation_id = create_conversation()

    client.cookies.clear()
    register_and_login(unique_username())

    response = client.post(
        f"/conversations/{conversation_id}/messages/stream",
        json={"content": "let me in"},
    )

    assert response.status_code == 404


def test_ai_status_requires_login_then_reports_echo():
    client.cookies.clear()
    assert client.get("/ai/status").status_code == 401

    register_and_login(unique_username())
    status = client.get("/ai/status")

    assert status.status_code == 200
    assert status.json()["provider"] == "echo"
    assert status.json()["state"] == "ready"
