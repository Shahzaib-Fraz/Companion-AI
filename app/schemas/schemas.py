from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class ChatRequest(BaseModel):
    message: str
    channel: str = "web"

class ChatResponse(BaseModel):
    response: str
    access_token: Optional[str] = None
    user_id: Optional[int] = None

class ReminderResponse(BaseModel):
    id: int
    content: str
    scheduled_at: datetime
