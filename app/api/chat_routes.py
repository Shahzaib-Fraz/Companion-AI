"""Chat Routes - Issue #19 & #20 Fixed"""
from fastapi import APIRouter, Depends, Header, HTTPException, Request  # ✅ ADD Request HERE
from sqlalchemy.orm import Session
import logging

from app.db.database import get_db
from app.services.chat_service import chat_service
from app.core.security import verify_access_token
from app.schemas.schemas import ChatRequest, ChatResponse, PIIMasker
from slowapi import Limiter
from slowapi.util import get_remote_address

logger = logging.getLogger(__name__)
router = APIRouter()

limiter = Limiter(key_func=get_remote_address)


@router.post("/message", response_model=ChatResponse)
@limiter.limit("10/minute")
async def send_message(
    request: Request,  # ✅ ADD THIS - Starlette Request for rate limiter
    chat_request: ChatRequest,  # ✅ RENAME from `request` to `chat_request`
    db: Session = Depends(get_db),
    authorization: str = Header(None),
):
    """
    Send a chat message.
    
    FIXED Issue #19: Rate limited to 10 requests/minute per IP
    FIXED Issue #20: Message validated (1-2000 chars), channel restricted (web/whatsapp)
    
    Args:
        request: Starlette Request (injected by slowapi rate limiter)
        chat_request: ChatRequest with validated message and channel
        db: Database session
        authorization: Bearer token for authentication
        
    Returns:
        ChatResponse with AI response
        
    Raises:
        HTTPException(401): Invalid or missing token
        HTTPException(422): Invalid message or channel (Pydantic validation)
        HTTPException(429): Rate limit exceeded (10/minute)
    """
    # Validate authorization header
    if not authorization or not authorization.startswith("Bearer "):
        logger.warning("❌ Chat request without authorization header")
        raise HTTPException(status_code=401, detail="Missing authorization token")

    # Verify token
    user_id = verify_access_token(authorization.replace("Bearer ", ""))
    if user_id is None:
        logger.warning("❌ Chat request with invalid/expired token")
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # ✅ Log with masked message (Issue #18 bonus)
    logger.info(
        "📨 Chat message received",
        extra={
            "user_id": user_id,
            "channel": chat_request.channel,  # ✅ Use chat_request instead
            "message_preview": PIIMasker.mask_message(chat_request.message),
            "request_id": chat_request.request_id,
        }
    )

    # ✅ Issue #20: Use chat_request.message and chat_request.channel
    response = await chat_service.process_message(
        db,
        chat_request.message,  # ✅ Use chat_request instead
        user_id,
        chat_request.channel,  # ✅ Use chat_request instead
    )

    # ✅ Log response sent
    logger.info(
        "✅ Chat response sent",
        extra={
            "user_id": user_id,
            "channel": chat_request.channel,  # ✅ Use chat_request instead
            "response_length": len(response.get("response", "")),
            "request_id": chat_request.request_id,  # ✅ Use chat_request instead
        }
    )

    return ChatResponse(
        response=response.get("response", "Sorry, I couldn't process that."),
        user_id=user_id,
        request_id=chat_request.request_id,  # ✅ Use chat_request instead
    )