from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.repositories.user_repository import UserRepository
from app.core.security import verify_access_token
router = APIRouter()

def get_user_id(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return verify_access_token(authorization.replace("Bearer ", ""))

@router.get("/profile")
def get_profile(db: Session = Depends(get_db), user_id: int = Depends(get_user_id)):
    if not user_id:
        return {"error": "Unauthorized"}
    profile = UserRepository.get_or_create_profile(db, user_id)
    return {"display_name": profile.display_name, "language": profile.language, "timezone": profile.timezone, "account_tier": profile.account_tier}
