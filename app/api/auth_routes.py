"""Authentication routes"""
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr, Field

from app.db.database import get_db
from app.core.security import create_access_token, verify_access_token, verify_password
from app.repositories.user_repository import UserRepository

router = APIRouter()


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


@router.post("/signup")
def signup(payload: SignupRequest, db: Session = Depends(get_db)):
    existing = UserRepository.get_by_email(db, payload.email)
    if existing:
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    user = UserRepository.create(db, email=payload.email, password=payload.password)
    UserRepository.get_or_create_profile(db, user.id)
    token = create_access_token(user.id)
    return {"access_token": token, "user_id": user.id}


@router.post("/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = UserRepository.get_by_email(db, payload.email)
    if not user or not user.password_hash or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_access_token(user.id)
    return {"access_token": token, "user_id": user.id}


@router.get("/verify")
def verify_token(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        return {"valid": False}
    token = authorization.replace("Bearer ", "")
    user_id = verify_access_token(token)
    return {"valid": user_id is not None, "user_id": user_id}