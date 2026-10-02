from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.backend.auth.service import (
    InvalidCredentialsError,
    UsernameAlreadyExistsError,
    authenticate_user,
    create_user,
)
from app.backend.database import get_db


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

    return {
        "id": user.id,
        "username": user.username,
    }
