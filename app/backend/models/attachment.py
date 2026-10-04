import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.backend.database import Base


class Attachment(Base):
    """A file the user attached to a conversation.

    Only the extracted text is stored (not the original file), so deleting a
    conversation removes everything that came from the upload.
    """

    __tablename__ = "attachments"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )

    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    filename: Mapped[str] = mapped_column(String(255), nullable=False)

    kind: Mapped[str] = mapped_column(String(10), nullable=False)

    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    text: Mapped[str] = mapped_column(Text, nullable=False)

    char_count: Mapped[int] = mapped_column(Integer, nullable=False)

    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    conversation = relationship(
        "Conversation",
        back_populates="attachments",
    )
