from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class ConversationCreate(BaseModel):
    title: str = "新对话"
    is_private: bool = False


class ConversationUpdate(BaseModel):
    title: Optional[str] = None
    is_private: Optional[bool] = None


class ConversationResponse(BaseModel):
    id: int
    user_id: int
    title: str
    is_private: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MessageResponse(BaseModel):
    id: int
    conversation_id: int
    role: str
    content: str
    file_url: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class GenerateRequest(BaseModel):
    conversation_id: Optional[int] = None
    prompt: str
    aspect_ratio: str = "1:1"
    is_private: Optional[bool] = None  # 隐私模式：true 时生成的图不会进公开画廊


class GenerateResponse(BaseModel):
    id: int
    conversation_id: Optional[int] = None
    image_url: str
    thumbnail_url: Optional[str] = None
    prompt: str
    aspect_ratio: str
    is_private: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


class GenerateTaskCreate(BaseModel):
    """异步生图：立刻返回 task_id，前端轮询。"""
    task_id: str
    status: str = "pending"  # pending / running / done / failed


class GenerateTaskStatus(BaseModel):
    task_id: str
    status: str  # pending / running / done / failed
    image: Optional[GenerateResponse] = None
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
