from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.backend.auth.service import (
    InvalidCredentialsError,
    UsernameAlreadyExistsError,
    authenticate_user,
    create_user,
)
from app.backend.auth.dependencies import get_current_user
from app.backend.auth.session import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE,
    create_session,
)
from app.backend.database import get_db
from app.backend.models import User



router = APIRouter(prefix="/auth", tags=["Authentication"])


class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(
    payload: RegisterRequest,
    db: Session = Depends(get_db),
):
    try:
        user = create_user(
            db,
            username=payload.username,
            password=payload.password,
        )
    except UsernameAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    return {
        "id": user.id,
        "username": user.username,
    }


@router.post("/login")
def login(
    payload: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    try:
        user = authenticate_user(
            db,
            username=payload.username,
            password=payload.password,
        )
    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    session_token = create_session(user.id)

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,
    )

    return {
        "id": user.id,
        "username": user.username,
    }

@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return {
        "id": user.id,
        "username": user.username,
    }


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
    )

    return {"status": "logged out"}

