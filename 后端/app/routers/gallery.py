from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.database import get_db
from app.models.generated_image import GeneratedImage
from app.models.user import User
from app.schemas.image import GalleryItemResponse

router = APIRouter(prefix="/api/gallery", tags=["gallery"])


@router.get("", response_model=list[GalleryItemResponse])
async def list_gallery(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(GeneratedImage)
        .join(User, GeneratedImage.user_id == User.id)
        .options(joinedload(GeneratedImage.user))
        .where(GeneratedImage.is_private == False)
        .order_by(GeneratedImage.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    images = result.unique().scalars().all()

    return [
        GalleryItemResponse(
            id=img.id,
            user_id=img.user_id,
            user_name=img.user.full_name or img.user.email.split("@")[0],
            user_avatar=img.user.avatar_url,
            prompt=img.prompt,
            image_url=img.image_url,
            thumbnail_url=img.thumbnail_url,
            aspect_ratio=img.aspect_ratio,
            created_at=img.created_at,
        )
        for img in images
    ]
