from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.database import get_db
from app.dependencies import admin_required
from app.models.generated_image import GeneratedImage
from app.models.user import User
from app.schemas.user import UserResponse

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/stats")
async def get_stats(
    current_user: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db),
):
    total = (await db.execute(select(func.count(User.id)))).scalar()
    admin_count = (await db.execute(select(func.count(User.id)).where(User.role == "admin"))).scalar()
    month_ago = datetime.now(timezone.utc) - timedelta(days=30)
    users_this_month = (
        await db.execute(select(func.count(User.id)).where(User.created_at >= month_ago))
    ).scalar()
    total_images = (await db.execute(select(func.count(GeneratedImage.id)))).scalar()
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    images_today = (
        await db.execute(
            select(func.count(GeneratedImage.id)).where(GeneratedImage.created_at >= today_start)
        )
    ).scalar()
    # 账号池统计（in-memory，O(1)）
    from app.services.gpt_pool.account_service import account_service
    pool = account_service.stats()
    return {
        "total_users": total,
        "admin_count": admin_count,
        "users_this_month": users_this_month,
        "total_images": total_images,
        "images_today": images_today,
        "pool_total_quota": pool["pool_total_quota"],
        "pool_available_accounts": pool["pool_available_accounts"],
    }


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    skip: int = 0,
    limit: int = 50,
    current_user: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User).offset(skip).limit(limit).order_by(User.created_at.desc())
    )
    return result.scalars().all()


@router.patch("/users/{user_id}/role", response_model=UserResponse)
async def update_user_role(
    user_id: int,
    role: str,
    current_user: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db),
):
    if role not in ("user", "admin"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="角色只能是 user 或 admin")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    user.role = role
    await db.commit()
    await db.refresh(user)
    return user


@router.get("/images")
async def list_all_images(
    skip: int = 0,
    limit: int = 50,
    user_id: int | None = None,
    current_user: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db),
):
    q = (
        select(GeneratedImage)
        .options(joinedload(GeneratedImage.user))
    )
    if user_id is not None:
        q = q.where(GeneratedImage.user_id == user_id)
    result = await db.execute(
        q.order_by(GeneratedImage.created_at.desc()).offset(skip).limit(limit)
    )
    images = result.unique().scalars().all()
    return [
        {
            "id": img.id,
            "user_id": img.user_id,
            "user_name": img.user.full_name or img.user.email,
            "conversation_id": img.conversation_id,
            "prompt": img.prompt,
            "image_url": img.image_url,
            "thumbnail_url": img.thumbnail_url,
            "aspect_ratio": img.aspect_ratio,
            "is_private": img.is_private,
            "created_at": img.created_at.isoformat(),
        }
        for img in images
    ]


@router.delete("/images/{image_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_any_image(
    image_id: int,
    current_user: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(GeneratedImage).where(GeneratedImage.id == image_id))
    img = result.scalar_one_or_none()
    if not img:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="图片不存在")
    await db.delete(img)
    await db.commit()


@router.get("/usage-trend")
async def usage_trend(
    days: int = 7,
    current_user: User = Depends(admin_required),
    db: AsyncSession = Depends(get_db),
):
    start_date = datetime.now(timezone.utc) - timedelta(days=days - 1)
    start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)

    # SQLite: strftime for day grouping
    result = await db.execute(
        select(
            func.date(GeneratedImage.created_at).label("date"),
            func.count(GeneratedImage.id).label("count"),
        )
        .where(GeneratedImage.created_at >= start_date)
        .group_by(text("date"))
        .order_by(text("date"))
    )
    rows = {row.date: row.count for row in result.all()}

    # Fill missing days with 0
    trend = []
    for i in range(days):
        d = start_date + timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        trend.append({"date": d.strftime("%m/%d"), "count": rows.get(ds, 0)})
    return trend
