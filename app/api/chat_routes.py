from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.services.chat_service import chat_service
from app.core.security import verify_access_token
router = APIRouter()

@router.post("/message")
async def send_message(request_data: dict, db: Session = Depends(get_db), authorization: str = Header(None)):
    user_id = None
    if authorization and authorization.startswith("Bearer "):
        user_id = verify_access_token(authorization.replace("Bearer ", ""))
    
    response = await chat_service.process_message(db, request_data.get("message", ""), user_id, request_data.get("channel", "web"))
    return response
