from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import os
from urllib.parse import urlparse, unquote

from app.database import get_db
from app.dependencies import get_current_user, get_current_user_query_or_header
from app.models.generated_image import GeneratedImage
from app.models.user import User
from app.schemas.image import GeneratedImageResponse

router = APIRouter(prefix="/api/images", tags=["images"])


@router.get("", response_model=list[GeneratedImageResponse])
async def list_my_images(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(GeneratedImage)
        .where(GeneratedImage.user_id == current_user.id)
        .order_by(GeneratedImage.created_at.desc())
    )
    return result.scalars().all()


@router.get("/{image_id}/download")
async def download_image(
    image_id: int,
    current_user: User = Depends(get_current_user_query_or_header),
    db: AsyncSession = Depends(get_db),
):
    """下载图片。

    支持两种鉴权方式：
      1. Authorization: Bearer <token>（axios 调用）
      2. ?token=<jwt>（浏览器 window.open 新标签页时使用）

    返回方式：
      - image_url 是相对路径（/images/...）：直接 FileResponse 把本地文件以 attachment
        方式发回，触发浏览器下载并使用真实文件名
      - image_url 是绝对路径：保持原 RedirectResponse 行为
    """
    result = await db.execute(
        select(GeneratedImage).where(
            GeneratedImage.id == image_id, GeneratedImage.user_id == current_user.id
        )
    )
    img = result.scalar_one_or_none()
    if not img:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="图片不存在")

    parsed = urlparse(img.image_url or "")
    is_absolute = bool(parsed.scheme and parsed.netloc)

    if is_absolute:
        # 上游绝对 URL，无能为力，仍走 302
        return RedirectResponse(url=img.image_url)

    # 相对路径：解析为本地文件
    rel = unquote(parsed.path or img.image_url).lstrip("/")
    # 仅允许 images/ 与 uploads/ 这两个静态挂载点
    if not (rel.startswith("images/") or rel.startswith("uploads/")):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="图片路径不合法")
    base_dir = "data/images" if rel.startswith("images/") else "uploads"
    inner = rel.split("/", 1)[1]
    file_path = os.path.normpath(os.path.join(base_dir, inner))
    # 防穿越：确认仍在 base_dir 下
    if not os.path.abspath(file_path).startswith(os.path.abspath(base_dir) + os.sep) \
       and os.path.abspath(file_path) != os.path.abspath(base_dir):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="非法路径")
    if not os.path.exists(file_path):
        # 兜底：本地没找到就重定向走 nginx 静态挂载
        return RedirectResponse(url=img.image_url)

    filename = os.path.basename(file_path) or f"image-{image_id}.png"
    return FileResponse(
        file_path,
        media_type="image/png",
        filename=filename,
        headers={"Cache-Control": "private, max-age=0, must-revalidate"},
    )


@router.delete("/{image_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_image(
    image_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(GeneratedImage).where(
            GeneratedImage.id == image_id, GeneratedImage.user_id == current_user.id
        )
    )
    img = result.scalar_one_or_none()
    if not img:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="图片不存在")
    await db.delete(img)
    await db.commit()
