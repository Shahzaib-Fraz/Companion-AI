import re
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Literal
from datetime import datetime
from uuid import uuid4
import logging

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    # REQUIRED: message (1-2000 chars)
    message: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Message must be 1-2000 characters"
    )

    # REQUIRED: channel (no default!)
    channel: Literal["web", "whatsapp"] = Field(
        description="Channel must be 'web' or 'whatsapp'"
    )

    # OPTIONAL: request_id (auto-generated if not provided)
    request_id: str = Field(
        default_factory=lambda: str(uuid4()),
        max_length=100,
        description="Request ID for tracking (auto-generated if omitted)"
    )

    # Normalize "website" -> "web" before the Literal check runs,
    # since the rest of the codebase/DB uses "website" as the canonical value
    @field_validator('channel', mode='before')
    @classmethod
    def normalize_channel(cls, v):
        if v == "website":
            return "web"
        return v

    # Validator to ensure message is not just whitespace
    @field_validator('message')
    @classmethod
    def message_not_empty(cls, v):
        if not v.strip():
            raise ValueError('message cannot be empty or whitespace only')
        return v.strip()


class ChatResponse(BaseModel):
    response: str
    access_token: Optional[str] = None
    user_id: Optional[int] = None
    request_id: Optional[str] = None

    # FIX: chat_routes.py has always passed reminder_set=... into this
    # model's constructor - but this field didn't exist here, and
    # Pydantic's default extra='ignore' behavior means an unrecognized
    # keyword argument is silently dropped, not an error. The route
    # "worked" (200, no exception) while reminder_set never actually
    # reached the client in the JSON response, ever. P1-19's whole point
    # (reminder_set reflecting the truth) was never actually visible
    # outside the server process until this field existed.
    reminder_set: bool = False


class ReminderResponse(BaseModel):
    id: int
    content: str
    scheduled_at: datetime
    sent_at: Optional[datetime] = None


# =====================================================================
# ISSUE #18 FIX: PIIMasker - Masks sensitive data in logs
# =====================================================================

# FIX: these three patterns are what mask_message() below was missing.
# The class docstring already claimed "Never log raw phone numbers,
# emails, or message content" - true for mask_phone()/mask_email()
# individually, but mask_message() itself only ever truncated by
# length; it never redacted anything WITHIN the text. A 40-character
# message containing an email or phone number passed through
# unchanged. These patterns are intentionally general-purpose (not
# tuned to any one field format) since mask_message() is applied to
# arbitrary free-text message/reminder content, not a known-shape
# value like a login's phone field.
_EMAIL_IN_TEXT_RE = re.compile(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9.-]+')
_JWT_OR_TOKEN_RE = re.compile(r'\b[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b')
_BEARER_RE = re.compile(r'\bBearer\s+[A-Za-z0-9._-]+', re.IGNORECASE)
# 13-19 digit runs (with optional separators) first, so a card-length
# number is labeled distinctly rather than falling through to the
# looser phone pattern below.
_LONG_NUMBER_RE = re.compile(r'(?<!\d)(?:\d[ -]*?){13,19}(?!\d)')
# Phone-like: 7+ digits total, optionally with +, spaces, hyphens,
# parentheses. Deliberately looser than a strict phone-number grammar,
# since this is scanning free text, not validating a phone field.
_PHONE_IN_TEXT_RE = re.compile(r'(?<!\d)\+?\d[\d\-\s()]{6,}\d(?!\d)')


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
    def redact(text: str) -> str:
        """
        FIX: the actual redaction mask_message() was missing. Strips
        emails, JWT-shaped tokens, "Bearer ..." headers, and
        phone/card-like digit runs out of arbitrary free text, replacing
        each with a labeled placeholder rather than deleting silently -
        the label says what kind of thing was there, without keeping
        any of its value.
        """
        if not text:
            return text
        text = _JWT_OR_TOKEN_RE.sub('[REDACTED_TOKEN]', text)
        text = _BEARER_RE.sub('Bearer [REDACTED_TOKEN]', text)
        text = _EMAIL_IN_TEXT_RE.sub('[REDACTED_EMAIL]', text)
        text = _LONG_NUMBER_RE.sub('[REDACTED_NUMBER]', text)
        text = _PHONE_IN_TEXT_RE.sub('[REDACTED_PHONE]', text)
        return text

    @staticmethod
    def mask_message(message: str, length: int = 50) -> str:
        """
        Redact PII from the message, THEN show a preview (first N
        characters of the redacted text, not the raw original).

        FIX: this used to only truncate - "call me at 923001234567" (24
        chars) would pass through completely unredacted since it never
        even hit the length cutoff. Now redacts first, so the phone
        number above becomes "call me at [REDACTED_PHONE]" regardless
        of whether truncation ever kicks in.

        Example:
            "can you remind me to go for a walk" → "can you remind me to go for a walk"
            "email me at john@x.com" → "email me at [REDACTED_EMAIL]"
        """
        if not message:
            return "[EMPTY_MESSAGE]"

        redacted = PIIMasker.redact(message)
        return redacted[:length] + "..." if len(redacted) > length else redacted

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