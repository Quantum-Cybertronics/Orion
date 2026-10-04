import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.backend.ai.runtime import get_runtime
from app.backend.attachments.extract import (
    ExtractionError,
    UnsupportedFileType,
    extract_text,
)
from app.backend.attachments.service import (
    AttachmentLimitError,
    AttachmentNotFoundError,
    add_attachment,
    attachment_summary,
    delete_attachment,
    list_attachments,
)
from app.backend.auth.dependencies import get_current_user
from app.backend.config import MAX_UPLOAD_BYTES
from app.backend.database import get_db
from app.backend.messages.service import ConversationNotFoundError
from app.backend.models import User

router = APIRouter(
    prefix="/conversations/{conversation_id}/attachments",
    tags=["Attachments"],
)


def _not_found(detail: str = "Conversation not found.") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _too_large() -> HTTPException:
    limit_mb = MAX_UPLOAD_BYTES / (1024 * 1024)

    return HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail=f"File is too large (limit {limit_mb:g} MB).",
    )


@router.post("/", status_code=status.HTTP_201_CREATED)
async def upload(
    conversation_id: str,
    request: Request,
    filename: str = Query(..., min_length=1, max_length=255),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Attach a file. The request body is the raw file; the name is in the URL.

    (Raw bodies avoid needing a multipart-parsing dependency on the USB.)
    """
    declared = request.headers.get("content-length")

    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise _too_large()

    chunks: list[bytes] = []
    total = 0

    async for chunk in request.stream():
        total += len(chunk)

        if total > MAX_UPLOAD_BYTES:
            raise _too_large()

        chunks.append(chunk)

    data = b"".join(chunks)

    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The file is empty.",
        )

    try:
        # Extraction can take a while for big PDFs; keep it off the event loop.
        extracted = await asyncio.to_thread(extract_text, filename, data)
    except UnsupportedFileType as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(exc),
        ) from exc
    except ExtractionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    try:
        attachment = add_attachment(
            db,
            user_id=user.id,
            conversation_id=conversation_id,
            filename=filename,
            extracted=extracted,
            size_bytes=len(data),
            budget=get_runtime().file_budget(),
        )
    except ConversationNotFoundError as exc:
        raise _not_found() from exc
    except AttachmentLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        ) from exc

    return attachment_summary(attachment)


@router.get("/")
def list_all(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        attachments = list_attachments(
            db, user_id=user.id, conversation_id=conversation_id
        )
    except ConversationNotFoundError as exc:
        raise _not_found() from exc

    return [attachment_summary(item) for item in attachments]


@router.delete("/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(
    conversation_id: str,
    attachment_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        delete_attachment(
            db,
            user_id=user.id,
            conversation_id=conversation_id,
            attachment_id=attachment_id,
        )
    except ConversationNotFoundError as exc:
        raise _not_found() from exc
    except AttachmentNotFoundError as exc:
        raise _not_found("Attachment not found.") from exc

    return Response(status_code=status.HTTP_204_NO_CONTENT)
