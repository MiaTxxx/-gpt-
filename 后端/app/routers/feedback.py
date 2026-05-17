from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.database import get_db
from app.dependencies import admin_required, get_current_user
from app.models.feedback import Feedback, FeedbackReply
from app.models.user import User

router = APIRouter(tags=["feedback"])


# ===== Schemas =====

class FeedbackCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class ReplyCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class ReplyOut(BaseModel):
    id: int
    is_admin: bool
    author_name: Optional[str]
    content: str
    created_at: str

    model_config = {"from_attributes": True}


class FeedbackOut(BaseModel):
    """反馈线程概览（不带消息）。"""
    id: int
    user_id: Optional[int]
    user_name: Optional[str]
    content: str
    status: str
    has_admin_reply: bool
    user_unread_count: int
    reply_count: int
    last_activity: str
    created_at: str

    model_config = {"from_attributes": True}


class FeedbackDetail(BaseModel):
    """反馈线程详情（带所有回复）。"""
    id: int
    user_id: Optional[int]
    user_name: Optional[str]
    content: str
    status: str
    has_admin_reply: bool
    created_at: str
    replies: list[ReplyOut]


class StatusUpdate(BaseModel):
    status: Literal["pending", "replied", "closed"]


class InboxSummary(BaseModel):
    total_threads: int
    unread_count: int    # 当前用户尚未读的管理员回复总数
    pending_count: int   # 当前管理员待处理（pending 状态）的反馈数（仅管理员有效）


# ===== Helpers =====

def _user_display(u: Optional[User]) -> Optional[str]:
    if not u:
        return None
    return u.full_name or u.email


def _to_thread_out(f: Feedback) -> FeedbackOut:
    last_activity = f.updated_at or f.created_at
    return FeedbackOut(
        id=f.id,
        user_id=f.user_id,
        user_name=_user_display(f.user),
        content=f.content,
        status=f.status,
        has_admin_reply=f.has_admin_reply,
        user_unread_count=f.user_unread_count,
        reply_count=len(f.replies) if f.replies is not None else 0,
        last_activity=last_activity.isoformat(),
        created_at=f.created_at.isoformat(),
    )


def _to_reply_out(r: FeedbackReply) -> ReplyOut:
    return ReplyOut(
        id=r.id,
        is_admin=r.is_admin,
        author_name=_user_display(r.author),
        content=r.content,
        created_at=r.created_at.isoformat(),
    )


# ===== 公共：提交反馈（保持向后兼容） =====

@router.post("/api/feedback", status_code=status.HTTP_201_CREATED)
async def submit_feedback(data: FeedbackCreate, db: AsyncSession = Depends(get_db)):
    """匿名提交反馈。"""
    fb = Feedback(content=data.content)
    db.add(fb)
    await db.commit()
    return {"message": "感谢你的反馈！"}


@router.post("/api/auth/feedback", status_code=status.HTTP_201_CREATED)
async def submit_auth_feedback(
    data: FeedbackCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """登录用户提交反馈，返回 thread id 以便前端跳到收件箱。"""
    fb = Feedback(user_id=current_user.id, content=data.content)
    db.add(fb)
    await db.commit()
    await db.refresh(fb)
    return {"message": "感谢你的反馈！", "id": fb.id}


# ===== 用户侧：收件箱 =====

@router.get("/api/feedback/inbox", response_model=list[FeedbackOut])
async def my_feedback_threads(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """当前用户的所有反馈线程（按最后活跃倒序）。"""
    result = await db.execute(
        select(Feedback)
        .options(joinedload(Feedback.user), selectinload(Feedback.replies))
        .where(Feedback.user_id == current_user.id)
        .order_by(Feedback.updated_at.desc())
    )
    threads = result.unique().scalars().all()
    return [_to_thread_out(t) for t in threads]


@router.get("/api/feedback/inbox/summary", response_model=InboxSummary)
async def my_inbox_summary(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """收件箱概览：总数 + 未读数。"""
    total = await db.scalar(
        select(func.count(Feedback.id)).where(Feedback.user_id == current_user.id)
    )
    unread = await db.scalar(
        select(func.coalesce(func.sum(Feedback.user_unread_count), 0)).where(
            Feedback.user_id == current_user.id
        )
    )
    pending = 0
    if current_user.role == "admin":
        pending = await db.scalar(
            select(func.count(Feedback.id)).where(Feedback.status == "pending")
        ) or 0
    return InboxSummary(
        total_threads=int(total or 0),
        unread_count=int(unread or 0),
        pending_count=int(pending or 0),
    )


@router.get("/api/feedback/{thread_id}", response_model=FeedbackDetail)
async def get_feedback_thread(
    thread_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """查看一条反馈线程详情（用户只能看自己的，管理员可看所有）。"""
    result = await db.execute(
        select(Feedback)
        .options(
            joinedload(Feedback.user),
            selectinload(Feedback.replies).joinedload(FeedbackReply.author),
        )
        .where(Feedback.id == thread_id)
    )
    fb = result.unique().scalar_one_or_none()
    if not fb:
        raise HTTPException(status_code=404, detail="反馈不存在")

    is_owner = fb.user_id == current_user.id
    is_admin = current_user.role == "admin"
    if not (is_owner or is_admin):
        raise HTTPException(status_code=403, detail="无权访问")

    # 用户进入会话时清零未读
    if is_owner and fb.user_unread_count > 0:
        fb.user_unread_count = 0
        await db.commit()
        await db.refresh(fb)

    return FeedbackDetail(
        id=fb.id,
        user_id=fb.user_id,
        user_name=_user_display(fb.user),
        content=fb.content,
        status=fb.status,
        has_admin_reply=fb.has_admin_reply,
        created_at=fb.created_at.isoformat(),
        replies=[_to_reply_out(r) for r in fb.replies],
    )


@router.post("/api/feedback/{thread_id}/reply", response_model=ReplyOut, status_code=201)
async def reply_to_thread(
    thread_id: int,
    data: ReplyCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """在线程下追加回复。用户和管理员通用——按当前用户身份判定 is_admin。"""
    result = await db.execute(select(Feedback).where(Feedback.id == thread_id))
    fb = result.scalar_one_or_none()
    if not fb:
        raise HTTPException(status_code=404, detail="反馈不存在")

    is_owner = fb.user_id == current_user.id
    is_admin = current_user.role == "admin"
    if not (is_owner or is_admin):
        raise HTTPException(status_code=403, detail="无权回复")

    reply = FeedbackReply(
        feedback_id=thread_id,
        author_id=current_user.id,
        is_admin=is_admin,
        content=data.content,
    )
    db.add(reply)

    # 联动状态字段
    if is_admin:
        fb.has_admin_reply = True
        fb.status = "replied"
        # 给反馈所属用户加未读数（如果反馈有归属用户）
        if fb.user_id is not None:
            fb.user_unread_count = (fb.user_unread_count or 0) + 1
    else:
        # 用户追问，重新打开 pending 状态
        if fb.status == "closed":
            fb.status = "pending"

    await db.commit()
    await db.refresh(reply)

    # 加载 author 关系给响应
    result = await db.execute(
        select(FeedbackReply)
        .options(joinedload(FeedbackReply.author))
        .where(FeedbackReply.id == reply.id)
    )
    reply = result.unique().scalar_one()
    return _to_reply_out(reply)


# ===== 管理员侧 =====

@router.get("/api/admin/feedback", response_model=list[FeedbackOut])
async def admin_list_feedback(
    skip: int = 0,
    limit: int = 100,
    status_filter: Optional[Literal["pending", "replied", "closed"]] = None,
    _: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Feedback)
        .options(joinedload(Feedback.user), selectinload(Feedback.replies))
        .order_by(Feedback.updated_at.desc())
        .offset(skip)
        .limit(limit)
    )
    if status_filter:
        stmt = stmt.where(Feedback.status == status_filter)

    result = await db.execute(stmt)
    items = result.unique().scalars().all()
    return [_to_thread_out(t) for t in items]


@router.patch("/api/admin/feedback/{thread_id}/status", response_model=FeedbackOut)
async def admin_update_status(
    thread_id: int,
    data: StatusUpdate,
    _: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Feedback)
        .options(joinedload(Feedback.user), selectinload(Feedback.replies))
        .where(Feedback.id == thread_id)
    )
    fb = result.unique().scalar_one_or_none()
    if not fb:
        raise HTTPException(status_code=404, detail="反馈不存在")
    fb.status = data.status
    await db.commit()
    await db.refresh(fb)
    return _to_thread_out(fb)


@router.delete("/api/admin/feedback/{thread_id}", status_code=204)
async def admin_delete_thread(
    thread_id: int,
    _: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Feedback).where(Feedback.id == thread_id))
    fb = result.scalar_one_or_none()
    if not fb:
        raise HTTPException(status_code=404, detail="反馈不存在")
    await db.delete(fb)
    await db.commit()
