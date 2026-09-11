
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Literal
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


# =====================================================================
# ISSUE #20 FIX: ChatRequest - Now validates message length + channel type
# =====================================================================

class ChatRequest(BaseModel):
    # ✅ CHANGED: message now has validation (1-2000 chars)
    message: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Message must be 1-2000 characters"
    )
    
    # ✅ CHANGED: channel now restricted to "web" or "whatsapp" only
    channel: Literal["web", "whatsapp"] = Field(
        default="web",
        description="Channel must be 'web' or 'whatsapp'"
    )
    
    # ✅ NEW: Added request_id for idempotency
    request_id: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Optional request ID for tracking"
    )
    
    # ✅ NEW: Validator to ensure message is not just whitespace
    @field_validator('message')
    @classmethod
    def message_not_empty(cls, v):
        if not v.strip():
            raise ValueError('message cannot be empty or whitespace only')
        return v.strip()


class ChatResponse(BaseModel):
    # SAME as before
    response: str
    access_token: Optional[str] = None
    user_id: Optional[int] = None
    
    # ✅ NEW: Echo back request_id for idempotency
    request_id: Optional[str] = None


class ReminderResponse(BaseModel):
    # SAME as before
    id: int
    content: str
    scheduled_at: datetime
    
    # ✅ NEW: Added sent_at to track when reminder was actually sent
    sent_at: Optional[datetime] = None


# =====================================================================
# ISSUE #18 FIX: PIIMasker - Masks sensitive data in logs
# =====================================================================

class PIIMasker:
    """
    Utility to mask Personally Identifiable Information (PII) for safe logging.
    
    FIXED Issue #18: Never log raw phone numbers, emails, or message content
    
    Usage in logs:
        logger.info("Message received", extra={
            "phone_masked": PIIMasker.mask_phone(user.phone),
            "message_preview": PIIMasker.mask_message(message),
        })
    """
    
    @staticmethod
    def mask_phone(phone: str) -> str:
        """
        Mask phone number for logging.
        
        Example:
            +923037454400 → +92***454400
            923037454400 → 92***454400
        """
        if not phone or len(phone) < 5:
            return "[INVALID_PHONE]"
        
        # Keep first 3 chars and last 6 chars, mask middle
        return phone[:3] + "***" + phone[-6:]
    
    @staticmethod
    def mask_email(email: str) -> str:
        """
        Mask email address for logging.
        
        Example:
            user@example.com → u***@example.com
            augustine@gmail.com → a***@gmail.com
        """
        if not email or "@" not in email:
            return "[INVALID_EMAIL]"
        
        local_part, domain = email.split("@")
        # Keep first char of local part, mask the rest
        masked_local = local_part[0] + "***" if len(local_part) > 1 else local_part[0]
        return f"{masked_local}@{domain}"
    
    @staticmethod
    def mask_message(message: str, length: int = 50) -> str:
        """
        Show preview of message (first N characters only).
        
        Example:
            "can you remind me to go for a walk" → "can you remind me to go for a walk"
            "very long message that is..." → "very long message that is very long m..."
        """
        if not message:
            return "[EMPTY_MESSAGE]"
        
        return message[:length] + "..." if len(message) > length else message
    
    @staticmethod
    def mask_name(name: str) -> str:
        """
        Mask person's name for logging.
        
        Example:
            "John Doe" → "J***"
            "Augustine" → "A***"
        """
        if not name or len(name) < 1:
            return "[INVALID_NAME]"
        
        return name[0] + "***"


