import uuid

from fastapi.testclient import TestClient

from app.backend.main import app


client = TestClient(app)


def unique_username() -> str:
    return f"api_test_{uuid.uuid4().hex[:8]}"


def test_register_user():
    username = unique_username()

    response = client.post(
        "/auth/register",
        json={
            "username": username,
            "password": "TestPassword123!",
        },
    )

    assert response.status_code == 201

    data = response.json()

    assert data["username"] == username
    assert "id" in data
    assert "password" not in data
    assert "password_hash" not in data


def test_duplicate_registration_rejected():
    username = unique_username()

    first_response = client.post(
        "/auth/register",
        json={
            "username": username,
            "password": "TestPassword123!",
        },
    )

    assert first_response.status_code == 201

    second_response = client.post(
        "/auth/register",
        json={
            "username": username,
            "password": "AnotherPassword123!",
        },
    )

    assert second_response.status_code == 409


def test_login_success():
    username = unique_username()

    register_response = client.post(
        "/auth/register",
        json={
            "username": username,
            "password": "TestPassword123!",
        },
    )

    assert register_response.status_code == 201

    response = client.post(
        "/auth/login",
        json={
            "username": username,
            "password": "TestPassword123!",
        },
    )

    assert response.status_code == 200

    data = response.json()

    assert data["username"] == username
    assert "password" not in data
    assert "password_hash" not in data


def test_login_wrong_password_rejected():
    username = unique_username()

    register_response = client.post(
        "/auth/register",
        json={
            "username": username,
            "password": "CorrectPassword123!",
        },
    )

    assert register_response.status_code == 201

    response = client.post(
        "/auth/login",
        json={
            "username": username,
            "password": "WrongPassword123!",
        },
    )

    assert response.status_code == 401


def test_login_unknown_user_rejected():
    response = client.post(
        "/auth/login",
        json={
            "username": unique_username(),
            "password": "SomePassword123!",
        },
    )

    assert response.status_code == 401
