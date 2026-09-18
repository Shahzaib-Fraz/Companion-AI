import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.core.security import verify_access_token
from app.core.limiter import limiter
from app.services.chat_service import chat_service
from app.schemas.schemas import ChatRequest, ChatResponse, PIIMasker

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/message", response_model=ChatResponse)
@limiter.limit("10/minute")
async def send_message(
    request: Request,
    chat_request: ChatRequest,
    db: Session = Depends(get_db),
    authorization: str = Header(None),
):

    # Step 1: FIXED P1-19: Validate auth (401)
    if not authorization or not authorization.startswith("Bearer "):
        logger.warning("❌ Chat request without authorization header")
        raise HTTPException(
            status_code=401,
            detail="Missing authorization token"
        )

    token = authorization.replace("Bearer ", "")
    user_id = verify_access_token(token)

    if user_id is None:
        logger.warning("❌ Chat request with invalid token")
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token"
        )

    # Step 2: Input is auto-validated by Pydantic (400 if invalid)
    # message: 1-2000 chars
    # channel: 'web' or 'whatsapp'

    # Step 3: FIXED P1-18: Log with masked message
    logger.info(
        "📨 Chat message received",
        extra={
            "user_id": user_id,
            "channel": chat_request.channel,
            "message_length": len(chat_request.message),
            "masked": PIIMasker.mask_message(chat_request.message)[:50],
        }
    )

    # Step 4: Process message
    try:
        result = await chat_service.process_message(
            db=db,
            message=chat_request.message,
            user_id=user_id,
            channel=chat_request.channel,
        )

        # P1-19 FIX: this used to check result.get("error") against
        # "INVALID_MESSAGE"/"SERVICE_ERROR" - strings chat_service never
        # actually returned (it returns plain messages like "Empty
        # message", "Authentication required", "User not found"), so
        # neither branch ever fired and every service-level error came
        # back as a generic HTTP 200. chat_service now sets a stable
        # "error_code" field (see process_message), which this switches on.
        if isinstance(result, dict) and "error_code" in result:
            code = result["error_code"]

            if code == "INVALID_MESSAGE":
                raise HTTPException(
                    status_code=400,
                    detail=result.get("error", "Invalid message")
                )
            elif code == "UNAUTHENTICATED":
                raise HTTPException(
                    status_code=401,
                    detail=result.get("error", "Authentication required")
                )
            elif code == "USER_NOT_FOUND":
                raise HTTPException(
                    status_code=404,
                    detail=result.get("error", "User not found")
                )
            elif code == "LLM_UNAVAILABLE":
                # Not a client error, and the service already produced a
                # usable (apologetic) response text plus a truthful
                # reminder_set value - fall through and return it as a
                # normal response instead of a 5xx.
                pass
            else:
                logger.error(f"Chat service error: {result}")
                raise HTTPException(
                    status_code=500,
                    detail="Service error processing message"
                )

        # Step 5: Build response
        response = ChatResponse(
            response=result.get("response", "Sorry, I couldn't process that."),
            user_id=user_id,
            request_id=chat_request.request_id,
            reminder_set=result.get("reminder_set", False),
        )

        # FIXED P1-18: Log response sent
        logger.info(
            "✅ Chat response sent",
            extra={
                "user_id": user_id,
                "channel": chat_request.channel,
                "response_length": len(response.response),
                "request_id": chat_request.request_id,
            }
        )

        return response

    except HTTPException:
        raise  # Re-raise HTTP exceptions as-is

    except Exception as e:
        # FIXED P1-19: Return 500 (not 200) for unexpected errors
        logger.exception(f"❌ Unhandled error in chat: {e}")
        raise HTTPException(
            status_code=500,
            detail="Internal server error"
        )