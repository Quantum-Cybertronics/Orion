from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backend.auth.security import hash_password
from app.backend.auth.security import hash_password, verify_password

from app.backend.models import User



class UsernameAlreadyExistsError(Exception):
    pass


def create_user(
    db: Session,
    username: str,
    password: str,
    ) -> User:
    existing_user = db.scalar(
        select(User).where(User.username == username)
    )

    if existing_user is not None:
        raise UsernameAlreadyExistsError(
            "Username already exists."
        )

    user = User(
        username=username,
        password_hash=hash_password(password),
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    return user

class InvalidCredentialsError(Exception):
    pass


def authenticate_user(
    db: Session,
    username: str,
    password: str,
) -> User:
    user = db.scalar(
        select(User).where(User.username == username)
    )

    if user is None:
        raise InvalidCredentialsError(
            "Invalid username or password."
        )

    if not verify_password(password, user.password_hash):
        raise InvalidCredentialsError(
            "Invalid username or password."
        )

    return user

