from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class GeneratedImageResponse(BaseModel):
    id: int
    user_id: int
    conversation_id: Optional[int] = None
    prompt: str
    image_url: str
    thumbnail_url: Optional[str] = None
    aspect_ratio: str
    is_private: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class GalleryItemResponse(BaseModel):
    id: int
    user_id: int
    user_name: str
    user_avatar: Optional[str] = None
    prompt: str
    image_url: str
    thumbnail_url: Optional[str] = None
    aspect_ratio: str
    created_at: datetime

    model_config = {"from_attributes": True}
