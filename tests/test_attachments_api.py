import uuid

from fastapi.testclient import TestClient

from app.backend.ai.service import AIService
from app.backend.main import app

client = TestClient(app)
RAW = {"Content-Type": "application/octet-stream"}


def unique_username() -> str:
    return f"attach_api_{uuid.uuid4().hex[:10]}"


def register_and_login(username: str, password: str = "TestPassword123!"):
    assert client.post("/auth/register", json={"username": username, "password": password}).status_code == 201
    assert client.post("/auth/login", json={"username": username, "password": password}).status_code == 200


def create_conversation() -> str:
    response = client.post("/conversations/", json={})
    assert response.status_code == 200
    return response.json()["id"]


def upload(conversation_id: str, name: str, data: bytes):
    return client.post(
        f"/conversations/{conversation_id}/attachments/",
        params={"filename": name},
        content=data,
        headers=RAW,
    )


def fresh_conversation() -> str:
    client.cookies.clear()
    register_and_login(unique_username())
    return create_conversation()


def test_attachments_require_authentication():
    client.cookies.clear()

    assert upload(str(uuid.uuid4()), "a.txt", b"hi").status_code == 401
    assert client.get(f"/conversations/{uuid.uuid4()}/attachments/").status_code == 401


def test_upload_list_and_delete():
    conversation_id = fresh_conversation()

    response = upload(conversation_id, "story.txt", b"The dragon was named Ember.")

    assert response.status_code == 201
    summary = response.json()
    assert summary["filename"] == "story.txt"
    assert summary["char_count"] == len("The dragon was named Ember.")
    assert "text" not in summary

    listed = client.get(f"/conversations/{conversation_id}/attachments/").json()
    assert [item["id"] for item in listed] == [summary["id"]]

    deleted = client.delete(f"/conversations/{conversation_id}/attachments/{summary['id']}")
    assert deleted.status_code == 204
    assert client.get(f"/conversations/{conversation_id}/attachments/").json() == []

    assert client.delete(f"/conversations/{conversation_id}/attachments/{summary['id']}").status_code == 404


def test_other_users_cannot_touch_the_conversation():
    conversation_id = fresh_conversation()
    attachment_id = upload(conversation_id, "a.txt", b"secret").json()["id"]

    client.cookies.clear()
    register_and_login(unique_username())

    assert upload(conversation_id, "b.txt", b"x").status_code == 404
    assert client.get(f"/conversations/{conversation_id}/attachments/").status_code == 404
    assert client.delete(f"/conversations/{conversation_id}/attachments/{attachment_id}").status_code == 404


def test_unsupported_empty_and_binary_files():
    conversation_id = fresh_conversation()

    assert upload(conversation_id, "setup.exe", b"MZ").status_code == 415
    assert upload(conversation_id, "empty.txt", b"").status_code == 400

    binary = upload(conversation_id, "image.txt", b"\x00\x01\x02" * 100)
    assert binary.status_code == 400
    assert "binary" in binary.json()["detail"]


def test_docx_upload():
    import io
    import zipfile

    conversation_id = fresh_conversation()
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Hello from Word</w:t></w:r></w:p></w:body></w:document>",
        )

    response = upload(conversation_id, "doc.docx", buffer.getvalue())

    assert response.status_code == 201
    assert response.json()["kind"] == "docx"


def test_file_over_size_limit(monkeypatch):
    conversation_id = fresh_conversation()
    monkeypatch.setattr("app.backend.attachments.routes.MAX_UPLOAD_BYTES", 100)

    response = upload(conversation_id, "big.txt", b"x" * 500)

    assert response.status_code == 413
    assert "too large" in response.json()["detail"]


def test_file_too_big_for_context_window():
    conversation_id = fresh_conversation()

    response = upload(conversation_id, "novel.txt", ("word " * 12000).encode())

    assert response.status_code == 413
    assert "context window" in response.json()["detail"]


def test_sixth_file_is_refused():
    conversation_id = fresh_conversation()

    for index in range(5):
        assert upload(conversation_id, f"f{index}.txt", b"tiny").status_code == 201

    response = upload(conversation_id, "f5.txt", b"tiny")

    assert response.status_code == 413
    assert "at most" in response.json()["detail"]


def test_attached_text_reaches_the_model_but_not_the_transcript(monkeypatch):
    conversation_id = fresh_conversation()
    upload(conversation_id, "story.txt", b"The dragon was named Ember.")

    seen = {}
    original = AIService.stream_reply

    def spy(self, messages):
        seen["messages"] = messages
        return original(self, messages)

    monkeypatch.setattr(AIService, "stream_reply", spy)

    response = client.post(
        f"/conversations/{conversation_id}/messages/stream",
        json={"content": "What is the dragon called?"},
    )

    assert response.status_code == 200
    assert seen["messages"][0]["role"] == "system"
    assert "Ember" in seen["messages"][0]["content"]

    saved = client.get(f"/conversations/{conversation_id}/messages/").json()
    assert [m["role"] for m in saved] == ["user", "assistant"]


def test_deleting_the_conversation_removes_attachments():
    conversation_id = fresh_conversation()
    upload(conversation_id, "story.txt", b"hello")

    assert client.delete(f"/conversations/{conversation_id}").status_code in (200, 204)
    assert client.get(f"/conversations/{conversation_id}/attachments/").status_code == 404
