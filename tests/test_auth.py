import uuid

from sqlalchemy import select

from app.backend.auth.security import hash_password, verify_password
from app.backend.auth.service import (
    InvalidCredentialsError,
    UsernameAlreadyExistsError,
    authenticate_user,
    create_user,
)

from app.backend.database import SessionLocal
from app.backend.models import User


def test_password_hashing():
    password = "TestPassword123!"

    hashed_password = hash_password(password)

    assert hashed_password != password
    assert verify_password(password, hashed_password)
    assert not verify_password("WrongPassword!", hashed_password)


def test_create_user():
    db = SessionLocal()
    username = f"test_{uuid.uuid4().hex[:8]}"

    try:
        user = create_user(
            db,
            username=username,
            password="TestPassword123!",
        )

        assert user.id is not None
        assert user.username == username
        assert user.password_hash != "TestPassword123!"
        assert verify_password(
            "TestPassword123!",
            user.password_hash,
        )

    finally:
        saved_user = db.scalar(
            select(User).where(User.username == username)
        )

        if saved_user is not None:
            db.delete(saved_user)
            db.commit()

        db.close()


def test_duplicate_username_rejected():
    db = SessionLocal()
    username = f"test_{uuid.uuid4().hex[:8]}"

    try:
        create_user(
            db,
            username=username,
            password="TestPassword123!",
        )

        try:
            create_user(
                db,
                username=username,
                password="AnotherPassword123!",
            )
        except UsernameAlreadyExistsError:
            pass
        else:
            raise AssertionError(
                "Duplicate username was not rejected."
            )

    finally:
        saved_user = db.scalar(
            select(User).where(User.username == username)
        )

        if saved_user is not None:
            db.delete(saved_user)
            db.commit()

        db.close()

def test_authenticate_user():
    db = SessionLocal()
    username = f"test_{uuid.uuid4().hex[:8]}"
    password = "TestPassword123!"

    try:
        create_user(
            db,
            username=username,
            password=password,
        )

        user = authenticate_user(
            db,
            username=username,
            password=password,
        )

        assert user.username == username

    finally:
        saved_user = db.scalar(
            select(User).where(User.username == username)
        )

        if saved_user is not None:
            db.delete(saved_user)
            db.commit()

        db.close()


def test_authenticate_user_rejects_wrong_password():
    db = SessionLocal()
    username = f"test_{uuid.uuid4().hex[:8]}"

    try:
        create_user(
            db,
            username=username,
            password="CorrectPassword123!",
        )

        try:
            authenticate_user(
                db,
                username=username,
                password="WrongPassword123!",
            )
        except InvalidCredentialsError:
            pass
        else:
            raise AssertionError(
                "Invalid password was accepted."
            )

    finally:
        saved_user = db.scalar(
            select(User).where(User.username == username)
        )

        if saved_user is not None:
            db.delete(saved_user)
            db.commit()

        db.close()


def test_authenticate_user_rejects_unknown_username():
    db = SessionLocal()

    try:
        try:
            authenticate_user(
                db,
                username=f"does_not_exist_{uuid.uuid4().hex}",
                password="SomePassword123!",
            )
        except InvalidCredentialsError:
            pass
        else:
            raise AssertionError(
                "Unknown username was accepted."
            )

    finally:
        db.close()

