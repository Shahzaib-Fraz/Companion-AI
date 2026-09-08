from fastapi import APIRouter, Header
from app.core.security import verify_access_token
router = APIRouter()

@router.get("/verify")
def verify_token(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"valid": False}
    token = authorization.replace("Bearer ", "")
    user_id = verify_access_token(token)
    return {"valid": user_id is not None, "user_id": user_id}
