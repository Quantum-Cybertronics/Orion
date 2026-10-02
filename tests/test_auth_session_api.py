import uuid

from fastapi.testclient import TestClient

from app.backend.main import app


client = TestClient(app)


def unique_username() -> str:
    return f"session_test_{uuid.uuid4().hex[:8]}"


def register_user(username: str, password: str) -> None:
    response = client.post(
        "/auth/register",
        json={
            "username": username,
            "password": password,
        },
    )

    assert response.status_code == 201


def test_me_requires_authentication():
    client.cookies.clear()

    response = client.get("/auth/me")

    assert response.status_code == 401


def test_login_creates_session():
    client.cookies.clear()

    username = unique_username()
    password = "TestPassword123!"

    register_user(username, password)

    response = client.post(
        "/auth/login",
        json={
            "username": username,
            "password": password,
        },
    )

    assert response.status_code == 200

    assert "orion_session" in client.cookies

    session_cookie = client.cookies["orion_session"]

    assert session_cookie
    assert "password" not in session_cookie.lower()


def test_session_authenticates_me():
    client.cookies.clear()

    username = unique_username()
    password = "TestPassword123!"

    register_user(username, password)

    login_response = client.post(
        "/auth/login",
        json={
            "username": username,
            "password": password,
        },
    )

    assert login_response.status_code == 200

    response = client.get("/auth/me")

    assert response.status_code == 200

    data = response.json()

    assert data["username"] == username
    assert "password" not in data
    assert "password_hash" not in data


def test_logout_clears_session():
    client.cookies.clear()

    username = unique_username()
    password = "TestPassword123!"

    register_user(username, password)

    login_response = client.post(
        "/auth/login",
        json={
            "username": username,
            "password": password,
        },
    )

    assert login_response.status_code == 200

    print("CLIENT COOKIES:", client.cookies)
    print("LOGIN SET-COOKIE:", login_response.headers.get("set-cookie"))

    assert client.get("/auth/me").status_code == 200

    logout_response = client.post("/auth/logout")

    assert logout_response.status_code == 200

    assert client.get("/auth/me").status_code == 401
