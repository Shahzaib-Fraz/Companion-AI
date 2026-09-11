"""SQLAlchemy ORM Models"""
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from app.db.database import Base

class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    phone_number = Column(String(20), unique=True, index=True, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    password_hash = Column(String(255), nullable=True)
    
    
    profile = relationship("UserProfile", back_populates="user", uselist=False)
    conversations = relationship("Conversation", back_populates="user")
    messages = relationship("Message", back_populates="user")
    reminders = relationship("Reminder", back_populates="user")

class UserProfile(Base):
    __tablename__ = "user_profiles"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)
    display_name = Column(String(255), nullable=True)
    timezone = Column(String, nullable=True)
    language = Column(String, nullable=True)  
    account_tier = Column(String(20), nullable=True)  # free or premium
    onboarding_step = Column(Integer, default=0, nullable=False)
    # onboarding_completed = Column(Boolean, default=False, nullable=False)
    onboarding_completed = Column(Boolean, default=False, index=True)
    response_style = Column(String(50), default="normal")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    user = relationship("User", back_populates="profile")

class Conversation(Base):
    __tablename__ = "conversations"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    channel = Column(String(20), index=True)  # web or whatsapp
    title = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    user = relationship("User", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")

class Message(Base):
    __tablename__ = "messages"
    
    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    role = Column(String(20), index=True)  # user or assistant
    content = Column(Text, nullable=False)
    channel = Column(String(20))  # web or whatsapp
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    external_message_id = Column(
        String(255),
        unique=True,
        index=True,
        nullable=True
    )
    
    conversation = relationship("Conversation", back_populates="messages")
    user = relationship("User", back_populates="messages")

class Reminder(Base):
    __tablename__ = "reminders"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    content = Column(Text, nullable=False)
    scheduled_at = Column(DateTime, index=True, nullable=False)
    sent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    attempt_count = Column(Integer, default=0, nullable=False)
    last_attempt_at = Column(DateTime, nullable=True)
    last_failure_reason = Column(Text, nullable=True)
    
    user = relationship("User", back_populates="reminders")

class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    token_hash = Column(String(255), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
