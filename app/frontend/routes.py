from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates

from app.backend.auth.dependencies import get_current_user
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
def app_page(
    request: Request,
    user: User = Depends(get_current_user),
):
    return templates.TemplateResponse(
        request=request,
        name="app.html",
        context={
            "username": user.username,
        },
    )
