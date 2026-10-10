import json
import uuid

from fastapi.testclient import TestClient

from app.backend.ai.base import AIProviderError
from app.backend.ai.service import AIService
from app.backend.main import app

PASSWORD = "TestPassword123!"


def new_client() -> TestClient:
    """A logged-in client for a brand-new user (so tests never share chats)."""
    client = TestClient(app)
    username = f"chat_tools_{uuid.uuid4().hex[:10]}"

    assert client.post(
        "/auth/register", json={"username": username, "password": PASSWORD}
    ).status_code == 201
    assert client.post(
        "/auth/login", json={"username": username, "password": PASSWORD}
    ).status_code == 200

    return client


def new_conversation(client: TestClient) -> str:
    response = client.post("/conversations/", json={})
    assert response.status_code == 200
    return response.json()["id"]


def events_of(response) -> list[dict]:
    events = []

    for block in response.text.split("\n\n"):
        block = block.strip()

        if block.startswith("data:"):
            events.append(json.loads(block[len("data:"):].strip()))

    return events


def send(client: TestClient, conversation_id: str, text: str) -> list[dict]:
    response = client.post(
        f"/conversations/{conversation_id}/messages/stream", json={"content": text}
    )
    assert response.status_code == 200
    return events_of(response)


def messages_of(client: TestClient, conversation_id: str) -> list[dict]:
    response = client.get(f"/conversations/{conversation_id}/messages/")
    assert response.status_code == 200
    return response.json()


def titles(client: TestClient) -> list[str]:
    return [item["title"] for item in client.get("/conversations/").json()]


def make_failing_stream(monkeypatch, message="the model is down"):
    def failing(self, messages):
        raise AIProviderError(message)
        yield  # makes this a generator, like the real stream_reply

    monkeypatch.setattr(AIService, "stream_reply", failing)


# ------------------------------------------------------------------ rename

def test_rename_changes_the_title_everywhere():
    client = new_client()
    conversation_id = new_conversation(client)

    response = client.patch(
        f"/conversations/{conversation_id}", json={"title": "  Holiday   plans  "}
    )

    assert response.status_code == 200
    assert response.json()["title"] == "Holiday plans"
    assert client.get(f"/conversations/{conversation_id}").json()["title"] == "Holiday plans"
    assert "Holiday plans" in titles(client)


def test_rename_rejects_empty_and_overlong_titles():
    client = new_client()
    conversation_id = new_conversation(client)

    empty = client.patch(f"/conversations/{conversation_id}", json={"title": "   "})
    long = client.patch(f"/conversations/{conversation_id}", json={"title": "x" * 101})

    assert empty.status_code == 422 and "empty" in empty.json()["detail"]
    assert long.status_code == 422 and "100" in long.json()["detail"]
    assert client.get(f"/conversations/{conversation_id}").json()["title"] == "New conversation"


def test_a_custom_title_is_not_overwritten_by_the_first_message():
    client = new_client()
    conversation_id = new_conversation(client)

    client.patch(f"/conversations/{conversation_id}", json={"title": "My own name"})
    send(client, conversation_id, "something completely different")

    assert client.get(f"/conversations/{conversation_id}").json()["title"] == "My own name"


def test_rename_is_private_and_needs_login():
    owner = new_client()
    conversation_id = new_conversation(owner)

    stranger = new_client()
    assert stranger.patch(
        f"/conversations/{conversation_id}", json={"title": "mine now"}
    ).status_code == 404

    assert TestClient(app).patch(
        f"/conversations/{conversation_id}", json={"title": "x"}
    ).status_code == 401

    assert owner.get(f"/conversations/{conversation_id}").json()["title"] == "New conversation"


# ------------------------------------------------------------------ search

def test_search_route_is_not_mistaken_for_a_conversation_id():
    client = new_client()

    response = client.get("/conversations/search", params={"q": "anything"})

    assert response.status_code == 200
    assert response.json() == []


def test_search_finds_a_chat_by_title_case_insensitively():
    client = new_client()
    conversation_id = new_conversation(client)
    client.patch(f"/conversations/{conversation_id}", json={"title": "Quarterly Budget"})

    results = client.get("/conversations/search", params={"q": "BUDGET"}).json()

    assert [r["id"] for r in results] == [conversation_id]
    assert results[0]["title"] == "Quarterly Budget"
    assert results[0]["snippet"] is None  # only the title matched


def test_search_finds_a_chat_by_message_text_with_a_snippet():
    client = new_client()
    conversation_id = new_conversation(client)
    other_id = new_conversation(client)

    send(client, conversation_id, "remember the zebracrossing plan for monday")
    send(client, other_id, "nothing relevant here")

    results = client.get("/conversations/search", params={"q": "ZebraCrossing"}).json()

    assert [r["id"] for r in results] == [conversation_id]
    assert "zebracrossing" in results[0]["snippet"].lower()


def test_search_returns_each_chat_once_even_with_many_matches():
    client = new_client()
    conversation_id = new_conversation(client)

    for _ in range(3):
        send(client, conversation_id, "banana banana banana")

    results = client.get("/conversations/search", params={"q": "banana"}).json()

    assert len(results) == 1


def test_search_treats_percent_and_underscore_literally():
    client = new_client()
    percent_id = new_conversation(client)
    plain_id = new_conversation(client)

    send(client, percent_id, "it is 100% certain")
    send(client, plain_id, "just plain words")

    percent = client.get("/conversations/search", params={"q": "100%"}).json()
    wildcard = client.get("/conversations/search", params={"q": "%"}).json()
    underscore = client.get("/conversations/search", params={"q": "_"}).json()

    assert [r["id"] for r in percent] == [percent_id]
    assert [r["id"] for r in wildcard] == [percent_id]   # not "match everything"
    assert underscore == []


def test_search_only_sees_your_own_chats():
    owner = new_client()
    conversation_id = new_conversation(owner)
    send(owner, conversation_id, "the secret word is pomegranate")

    stranger = new_client()

    assert stranger.get("/conversations/search", params={"q": "pomegranate"}).json() == []
    assert len(owner.get("/conversations/search", params={"q": "pomegranate"}).json()) == 1


def test_search_validates_the_query_and_needs_login():
    client = new_client()

    assert client.get("/conversations/search").status_code == 422
    assert client.get("/conversations/search", params={"q": ""}).status_code == 422
    assert client.get("/conversations/search", params={"q": "x" * 101}).status_code == 422
    assert client.get("/conversations/search", params={"q": "   "}).json() == []
    assert TestClient(app).get("/conversations/search", params={"q": "x"}).status_code == 401


# ------------------------------------------------------------------ export

def test_export_downloads_the_chat_as_markdown():
    client = new_client()
    conversation_id = new_conversation(client)
    client.patch(f"/conversations/{conversation_id}", json={"title": "Trip to Goa"})
    send(client, conversation_id, "Plan a trip please")

    response = client.get(f"/conversations/{conversation_id}/export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "attachment" in response.headers["content-disposition"]
    assert "orion-trip-to-goa.md" in response.headers["content-disposition"]

    body = response.text
    assert body.startswith("# Trip to Goa")
    assert "## You\n\nPlan a trip please" in body
    assert "## ORION" in body
    assert "2 messages" in body


def test_export_of_an_empty_chat_still_works():
    client = new_client()
    conversation_id = new_conversation(client)

    response = client.get(f"/conversations/{conversation_id}/export")

    assert response.status_code == 200
    assert "# New conversation" in response.text


def test_export_is_private_and_needs_login():
    owner = new_client()
    conversation_id = new_conversation(owner)

    assert new_client().get(f"/conversations/{conversation_id}/export").status_code == 404
    assert TestClient(app).get(f"/conversations/{conversation_id}/export").status_code == 401
    assert owner.get(f"/conversations/{uuid.uuid4()}/export").status_code == 404


# -------------------------------------------------------------- regenerate

def regenerate(client: TestClient, conversation_id: str):
    return client.post(f"/conversations/{conversation_id}/messages/regenerate")


def test_regenerate_replaces_the_last_answer():
    client = new_client()
    conversation_id = new_conversation(client)
    send(client, conversation_id, "tell me something")

    before = messages_of(client, conversation_id)
    response = regenerate(client, conversation_id)
    events = events_of(response)
    after = messages_of(client, conversation_id)

    assert response.status_code == 200
    assert events[-1]["type"] == "done" and events[-1]["id"]
    assert not any(e["type"] == "user_message" for e in events)

    assert [m["role"] for m in after] == ["user", "assistant"]   # no duplicates
    assert after[0]["id"] == before[0]["id"]                      # question untouched
    assert after[1]["id"] != before[1]["id"]                      # answer replaced
    assert after[1]["id"] == events[-1]["id"]


def test_regenerate_only_touches_the_last_answer():
    client = new_client()
    conversation_id = new_conversation(client)
    send(client, conversation_id, "first question")
    send(client, conversation_id, "second question")

    before = messages_of(client, conversation_id)
    regenerate(client, conversation_id)
    after = messages_of(client, conversation_id)

    assert [m["id"] for m in after[:3]] == [m["id"] for m in before[:3]]
    assert after[3]["id"] != before[3]["id"]
    assert len(after) == 4


def test_a_failed_regeneration_keeps_the_old_answer(monkeypatch):
    client = new_client()
    conversation_id = new_conversation(client)
    send(client, conversation_id, "keep my answer")
    before = messages_of(client, conversation_id)

    make_failing_stream(monkeypatch)
    events = events_of(regenerate(client, conversation_id))

    assert events[-1] == {"type": "error", "detail": "the model is down"}
    assert messages_of(client, conversation_id) == before


def test_regenerate_retries_when_the_reply_was_never_saved(monkeypatch):
    client = new_client()
    conversation_id = new_conversation(client)

    with monkeypatch.context() as patch:
        make_failing_stream(patch)
        send(client, conversation_id, "this reply fails")

    assert [m["role"] for m in messages_of(client, conversation_id)] == ["user"]

    events = events_of(regenerate(client, conversation_id))

    assert events[-1]["type"] == "done"
    assert [m["role"] for m in messages_of(client, conversation_id)] == ["user", "assistant"]


def test_regenerate_with_nothing_to_answer_is_a_conflict():
    client = new_client()
    conversation_id = new_conversation(client)

    response = regenerate(client, conversation_id)

    assert response.status_code == 409
    assert "no message" in response.json()["detail"]


def test_regenerate_is_private_and_needs_login():
    owner = new_client()
    conversation_id = new_conversation(owner)
    send(owner, conversation_id, "hello")

    assert regenerate(new_client(), conversation_id).status_code == 404
    assert TestClient(app).post(
        f"/conversations/{conversation_id}/messages/regenerate"
    ).status_code == 401
    assert regenerate(owner, str(uuid.uuid4())).status_code == 404
