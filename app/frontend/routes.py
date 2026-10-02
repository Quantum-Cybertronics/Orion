from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.backend.auth.session import (
    SESSION_COOKIE_NAME,
    get_user_id,
)
from app.backend.database import SessionLocal
from app.backend.models import User


BASE_DIR = Path(__file__).resolve().parents[2]
FRONTEND_DIR = BASE_DIR / "frontend"

templates = Jinja2Templates(
    directory=str(FRONTEND_DIR / "templates"),
)

router = APIRouter(tags=["Frontend"])


@router.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
    )


@router.get("/signup")
async def signup_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="signup.html",
    )


@router.get("/app")
async def app_page(request: Request):
    session_token = request.cookies.get(SESSION_COOKIE_NAME)

    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
        )

    user_id = get_user_id(session_token)

    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
        )

    db: Session = SessionLocal()

    try:
        user = db.get(User, user_id)

        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated.",
            )

        return templates.TemplateResponse(
            request=request,
            name="app.html",
            context={
                "username": user.username,
            },
        )
    finally:
        db.close()
