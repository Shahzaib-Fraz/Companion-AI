from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session
from datetime import datetime
from app.db.database import get_db
from app.repositories.reminder_repository import ReminderRepository
from app.core.security import verify_access_token
router = APIRouter()

def get_user_id(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return verify_access_token(authorization.replace("Bearer ", ""))

@router.post("/")
def create_reminder(content: str, scheduled_at: str, db: Session = Depends(get_db), user_id: int = Depends(get_user_id)):
    if not user_id:
        return {"error": "Unauthorized"}
    scheduled = datetime.fromisoformat(scheduled_at)
    reminder = ReminderRepository.create(db, user_id, content, scheduled)
    return {"id": reminder.id, "status": "created"}
