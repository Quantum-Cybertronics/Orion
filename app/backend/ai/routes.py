from fastapi import APIRouter, Depends

from app.backend.ai.runtime import get_runtime
from app.backend.auth.dependencies import get_current_user
from app.backend.models import User

router = APIRouter(prefix="/ai", tags=["AI"])


@router.get("/status")
def ai_status(user: User = Depends(get_current_user)):
    """Which AI backend is active and whether it is ready to answer."""
    return get_runtime().status()
