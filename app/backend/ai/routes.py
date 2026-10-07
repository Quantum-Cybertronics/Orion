from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.backend.ai.runtime import ModelSwitchError, get_runtime
from app.backend.auth.dependencies import get_current_user
from app.backend.models import User

router = APIRouter(prefix="/ai", tags=["AI"])


@router.get("/status")
def ai_status(user: User = Depends(get_current_user)):
    """Which AI backend is active, whether it is ready, and the models on offer."""
    return get_runtime().status()


class SelectModelRequest(BaseModel):
    model: str


@router.post("/model")
def select_model(
    payload: SelectModelRequest,
    user: User = Depends(get_current_user),
):
    """Switch the chat model. It loads in the background; poll /ai/status."""
    try:
        return get_runtime().select_model(payload.model)
    except ModelSwitchError as exc:
        code = (
            status.HTTP_404_NOT_FOUND
            if exc.reason == "not_found"
            else status.HTTP_409_CONFLICT
        )

        raise HTTPException(status_code=code, detail=str(exc)) from exc
