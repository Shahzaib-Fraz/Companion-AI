"""SQLAlchemy ORM models with all P0 fixes"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, ForeignKey, JSON, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from app.db.database import Base


class User(Base):
    """User account"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    phone_number = Column(String(20), unique=True, nullable=True, index=True)
    password_hash = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    profile = relationship("UserProfile", back_populates="user", uselist=False)
    conversations = relationship("Conversation", back_populates="user", cascade="all, delete-orphan")
    messages = relationship("Message", back_populates="user", cascade="all, delete-orphan")
    reminders = relationship("Reminder", back_populates="user", cascade="all, delete-orphan")
    refresh_tokens = relationship("RefreshToken", back_populates="user", cascade="all, delete-orphan")


class UserProfile(Base):
    """User profile information"""
    __tablename__ = "user_profiles"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    display_name = Column(String(100), nullable=True)
    timezone = Column(String(50), default="UTC")  # P0-11: Store timezone
    language = Column(String(10), default="en")
    account_tier = Column(String(20), default="free")
    onboarding_completed = Column(Boolean, default=False)
    onboarding_step = Column(Integer, default=0)

    # P1-17 FIX: WhatsApp phone ownership verification. A phone number
    # typed into onboarding lands here FIRST, alongside a short-lived
    # code sent to that number over WhatsApp - it is only copied to
    # User.phone_number (the field notifications actually go to) once the
    # person proves they received that code. See onboarding_service and
    # chat_service._handle_onboarding's STEP_WHATSAPP handling.
    pending_phone = Column(String(20), nullable=True)
    pending_phone_code = Column(String(10), nullable=True)
    pending_phone_code_expires_at = Column(DateTime, nullable=True)
    pending_phone_attempts = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="profile")


class RefreshToken(Base):
    """Refresh tokens for JWT"""
    __tablename__ = "refresh_tokens"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    token = Column(String(500), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="refresh_tokens")


# P1-13 FIX: Unique constraint for (user_id, channel)
class Conversation(Base):
    """Conversation per user and channel"""
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint('user_id', 'channel', name='uq_conversations_user_channel'),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    channel = Column(String(20), default="web")  # web, whatsapp, etc
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")


class Message(Base):
    """Chat messages"""
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    external_message_id = Column(String(255), unique=True, nullable=True, index=True)  # P0-6: Idempotency
    role = Column(String(20), nullable=False)  # user or assistant
    content = Column(Text, nullable=False)
    channel = Column(String(20), default="web")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    conversation = relationship("Conversation", back_populates="messages")
    user = relationship("User", back_populates="messages")


# P0-8 FIX: Webhook event persistence for durability
class WebhookEvent(Base):
    """Persisted webhook events for durability"""
    __tablename__ = "webhook_events"

    id = Column(Integer, primary_key=True)
    provider = Column(String(20), nullable=False)  # whatsapp, email, etc
    provider_event_id = Column(String(255), unique=True, nullable=False, index=True)
    payload = Column(JSON, nullable=False)
    status = Column(String(20), default="RECEIVED")  # RECEIVED, PROCESSING, PROCESSED, FAILED
    processed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# P0-6 FIX: Webhook message idempotency tracking
class WebhookMessage(Base):
    """Track processed webhook messages for idempotency"""
    __tablename__ = "webhook_messages"

    id = Column(Integer, primary_key=True)
    webhook_event_id = Column(Integer, ForeignKey("webhook_events.id", ondelete="CASCADE"))
    provider_message_id = Column(String(255), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    status = Column(String(20), default="RECEIVED")  # RECEIVED, PROCESSING, PROCESSED
    created_at = Column(DateTime, default=datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)


# P0-4, P0-5 FIX: Reminder state machine instead of just sent_at
class Reminder(Base):
    """Reminders with state machine"""
    __tablename__ = "reminders"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    content = Column(Text, nullable=False)
    scheduled_at = Column(DateTime, nullable=False, index=True)  # UTC time
    timezone = Column(String(50), nullable=False, server_default="UTC")  # User's timezone for display

    # P0-4: State machine: PENDING -> PROCESSING -> SENT / PARTIALLY_SENT / FAILED
    status = Column(String(20), default="PENDING", index=True)

    # P0-5: Per-channel tracking
    email_status = Column(String(20), default="PENDING")
    whatsapp_status = Column(String(20), default="PENDING")

    # Retry tracking
    attempt_count = Column(Integer, default=0)
    last_attempt_at = Column(DateTime, nullable=True)
    last_failure_reason = Column(Text, nullable=True)

    # Original timestamps
    sent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Composite index for P0-5 atomic claims
    __table_args__ = (
        Index('ix_reminders_status_scheduled', 'status', 'scheduled_at'),
    )

    user = relationship("User", back_populates="reminders")


# P0-12 FIX: Memory summaries with cursor tracking
class MemorySummary(Base):
    """Conversation summaries for memory"""
    __tablename__ = "memory_summaries"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    summary_text = Column(Text, nullable=False)
    last_summarized_message_id = Column(Integer, nullable=False)  # Cursor for incremental
    token_count = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    user = relationship("User", back_populates="summaries")


class SummaryJob(Base):
    """Track summary generation jobs"""
    __tablename__ = "summary_jobs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status = Column(String(20), default="PENDING")  # PENDING, PROCESSING, COMPLETED, FAILED
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# P1-20 FIX: LLM usage tracking - now actually written to, by
# LLMService._record_usage() (app/services/llm_service.py), whenever a
# caller passes a usage_context. See chat_service.py and
# scheduler_worker.py for the call sites that populate it.
class LLMUsage(Base):
    """Track LLM API calls and costs"""
    __tablename__ = "llm_usage"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    model = Column(String(100), nullable=False)
    purpose = Column(String(50), nullable=False)  # chat, reminder_extraction, summarization, preference_extraction
    input_tokens = Column(Integer, nullable=False)
    output_tokens = Column(Integer, nullable=False)
    cost_usd = Column(Integer, nullable=False)  # Store in cents to avoid floats
    latency_ms = Column(Integer, nullable=False)
    status = Column(String(20), default="SUCCESS")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


# Add relationships to User
User.summaries = relationship("MemorySummary", back_populates="user", cascade="all, delete-orphan")