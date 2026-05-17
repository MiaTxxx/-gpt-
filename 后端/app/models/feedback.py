from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.user import User


class Feedback(Base):
    """一条反馈线程（用户提交的一次反馈）。"""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # pending(待处理) / replied(已回复) / closed(已关闭)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    has_admin_reply: Mapped[bool] = mapped_column(Boolean, default=False)
    user_unread_count: Mapped[int] = mapped_column(Integer, default=0)  # 用户尚未读的回复数
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[Optional["User"]] = relationship("User")
    replies: Mapped[list["FeedbackReply"]] = relationship(
        "FeedbackReply",
        back_populates="feedback",
        cascade="all, delete-orphan",
        order_by="FeedbackReply.created_at",
    )


class FeedbackReply(Base):
    """反馈线程下的一条回复（双向：管理员/用户都能回）。"""

    __tablename__ = "feedback_replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    feedback_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("feedback.id", ondelete="CASCADE"), index=True
    )
    author_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)  # 标记这条回复是否由管理员发出
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    feedback: Mapped["Feedback"] = relationship("Feedback", back_populates="replies")
    author: Mapped[Optional["User"]] = relationship("User")
