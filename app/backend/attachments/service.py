from sqlalchemy.orm import Session

from app.backend.ai.tokens import FileBudget, estimate_tokens
from app.backend.attachments.context import load_attachments
from app.backend.attachments.extract import (
    ExtractedText,
    clean_filename,
)
from app.backend.messages.service import (
    ConversationNotFoundError,
    get_owned_conversation,
)
from app.backend.models import Attachment

MAX_ATTACHMENTS_PER_CONVERSATION = 5


class AttachmentNotFoundError(Exception):
    pass


class AttachmentLimitError(Exception):
    """Too many files, or too much text for the model's context window."""


def add_attachment(
    db: Session,
    user_id: str,
    conversation_id: str,
    filename: str,
    extracted: ExtractedText,
    size_bytes: int,
    budget: FileBudget,
) -> Attachment:
    get_owned_conversation(db, user_id=user_id, conversation_id=conversation_id)

    existing = load_attachments(db, conversation_id)

    if len(existing) >= MAX_ATTACHMENTS_PER_CONVERSATION:
        raise AttachmentLimitError(
            f"A conversation can have at most {MAX_ATTACHMENTS_PER_CONVERSATION} "
            "files. Remove one first."
        )

    tokens = estimate_tokens(extracted.text)
    used = sum(item.token_estimate for item in existing)
    remaining = max(0, budget.total_tokens - used)

    if tokens > remaining:
        raise AttachmentLimitError(
            f"This file is about {tokens:,} tokens, but only about "
            f"{remaining:,} more fit in the AI model's context window "
            f"({budget.ctx_size:,} tokens). Use a shorter file, remove another "
            "file, or raise ORION_LLAMA_CTX."
        )

    attachment = Attachment(
        conversation_id=conversation_id,
        filename=clean_filename(filename),
        kind=extracted.kind,
        size_bytes=size_bytes,
        text=extracted.text,
        char_count=len(extracted.text),
        token_estimate=tokens,
    )

    db.add(attachment)
    db.commit()
    db.refresh(attachment)

    return attachment


def list_attachments(
    db: Session,
    user_id: str,
    conversation_id: str,
) -> list[Attachment]:
    get_owned_conversation(db, user_id=user_id, conversation_id=conversation_id)

    return load_attachments(db, conversation_id)


def delete_attachment(
    db: Session,
    user_id: str,
    conversation_id: str,
    attachment_id: str,
) -> None:
    get_owned_conversation(db, user_id=user_id, conversation_id=conversation_id)

    attachment = db.get(Attachment, attachment_id)

    if attachment is None or attachment.conversation_id != conversation_id:
        raise AttachmentNotFoundError

    db.delete(attachment)
    db.commit()


def attachment_summary(attachment: Attachment) -> dict:
    """What the API returns: everything except the file's text."""
    return {
        "id": attachment.id,
        "filename": attachment.filename,
        "kind": attachment.kind,
        "size_bytes": attachment.size_bytes,
        "char_count": attachment.char_count,
        "token_estimate": attachment.token_estimate,
        "created_at": attachment.created_at.isoformat(),
    }
