import time

from app.backend.auth.session import (
    SESSION_MAX_AGE,
    create_session,
    get_user_id,
)


def test_create_and_validate_session():
    user_id = "9951c296-99fb-4253-8933-0dc55338f36d"

    token = create_session(user_id)

    assert token
    assert get_user_id(token) == user_id



def test_tampered_session_rejected():
    token = create_session(123)

    payload, timestamp, signature = token.split(".")

    tampered_signature = (
        "x" + signature[1:]
        if signature[0] != "x"
        else "y" + signature[1:]
    )

    tampered_token = ".".join(
        [payload, timestamp, tampered_signature]
    )

    assert get_user_id(tampered_token) is None



def test_invalid_session_rejected():
    assert get_user_id("not-a-valid-session") is None


def test_session_contains_no_plaintext_password():
    token = create_session(123)

    assert "password" not in token.lower()
