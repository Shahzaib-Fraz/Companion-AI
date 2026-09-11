"""WhatsApp Webhook Routes"""
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Header, HTTPException, Query, Request, Depends, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.schemas.schemas import PIIMasker 
from app.core.config import settings
from app.db.database import get_db
from app.models.database import Message
from app.services.chat_service import chat_service
from app.services.whatsapp_service import whatsapp_service
from app.repositories.user_repository import UserRepository

logger = logging.getLogger(__name__)

router = APIRouter()


def _verify_signature(raw_body: bytes, signature_header: str) -> bool:
    """
    Meta signs every POST webhook body with HMAC-SHA256 using your App
    Secret, sent as 'sha256=<hex>' in the X-Hub-Signature-256 header.
    
    Without this check, anyone who finds your ngrok/production URL can POST
    fake messages, phone numbers and message IDs and the backend will treat
    them as real WhatsApp traffic — triggering LLM calls, reminders, and
    outbound WhatsApp sends on your dime.
    
    Returns:
        True if signature is valid, False otherwise
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    
    expected = hmac.new(
        settings.WHATSAPP_APP_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    
    provided = signature_header.split("sha256=", 1)[1]
    
    # constant-time compare — a plain == leaks timing info an attacker can
    # use to guess the signature byte by byte.
    return hmac.compare_digest(expected, provided)


def _is_duplicate_message(db: Session, external_message_id: str) -> bool:
    """
    Check if message was already processed.
    
    FIXED (Issue #15): Checks external_message_id for idempotency
    Returns True if message already exists, False if new.
    
    Args:
        db: Database session
        external_message_id: WhatsApp message ID
        
    Returns:
        True if message already processed, False if new
    """
    if not external_message_id:
        return False
    
    try:
        existing = db.query(Message).filter(
            Message.external_message_id == external_message_id
        ).first()
        return existing is not None
    except Exception as e:
        logger.warning(f"Error checking for duplicate message: {e}")
        # On error, assume not duplicate (process it)
        return False


@router.post("/webhook/whatsapp")
async def whatsapp_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_hub_signature_256: str = Header(None, alias="X-Hub-Signature-256"),
):
    """
    Receive messages from WhatsApp Cloud API.
    
    FIXED (Issue #15): Checks external_message_id for duplicates
    FIXED (Issue #17): Returns appropriate HTTP status codes
    - 200: Malformed/non-actionable events (can't retry)
    - 401: Invalid signature
    - 500: Retryable failures (database, network)
    - 503: Service temporarily unavailable
    
    Args:
        request: FastAPI request
        db: Database session
        x_hub_signature_256: Webhook signature from Meta
        
    Returns:
        Status response
        
    Raises:
        HTTPException(401): Invalid webhook signature
        HTTPException(500): Retryable server error
    """
    raw_body = await request.body()

    # Validate webhook signature (Issue #17: Return 401 for invalid signature)
    if not _verify_signature(raw_body, x_hub_signature_256):
        logger.warning("❌ Rejected WhatsApp webhook: invalid or missing signature")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature"
        )

    # Parse JSON (Issue #17: Return 200 for malformed - can't retry)
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        # Not actionable, but also not our fault — 200 so Meta doesn't retry
        # a payload that will never parse.
        logger.warning("⚠️  WhatsApp webhook body was not valid JSON")
        return {"status": "ok", "reason": "malformed_json"}

    try:
        logger.info("📱 WhatsApp webhook received")

        # Parse message (handles malformed data gracefully)
        parsed_message = whatsapp_service.parse_webhook_message(body)

        if not parsed_message:
            logger.debug("ℹ️  No actionable message in webhook (likely a status update)")
            return {"status": "ok", "reason": "no_actionable_message"}

        phone_number = parsed_message["phone_number"]
        message_content = parsed_message["content"]
        message_id = parsed_message["message_id"]

        # ✅ FIXED Issue #18: Use PIIMasker for safe logging
        logger.info(
            "📱 WhatsApp message received",
            extra={
                "phone_masked": PIIMasker.mask_phone(phone_number),
                "message_id": message_id,
            }
        )

        # ========== IDEMPOTENCY CHECK (Issue #15) ==========
        # Check if message already processed to prevent duplicates
        if _is_duplicate_message(db, message_id):
            logger.info(f"✓ Message {message_id} already processed (idempotent)")
            return {
                "status": "ok",
                "reason": "duplicate_message",
                "message_processed": False
            }

        # Mark as read (Issue #13: Now async)
        try:
            await whatsapp_service.mark_as_read(message_id)
        except Exception as e:
            logger.warning(f"Failed to mark message as read: {e}")
            # Don't fail the whole webhook for this

        # ========== USER LOOKUP ==========
        user = UserRepository.get_by_phone(db, phone_number)

        if not user:
            # ✅ FIXED Issue #18: Use PIIMasker for safe logging
            logger.info(
                "ℹ️  Message from unlinked WhatsApp number",
                extra={
                    "phone_masked": PIIMasker.mask_phone(phone_number),
                }
            )
            try:
                # Issue #13: Now async
                await whatsapp_service.send_message(
                    phone_number,
                    "Hi! I don't recognize this number yet. Please sign up on the web "
                    "app first, then add this WhatsApp number during setup to link it.",
                )
            except Exception as e:
                logger.warning(f"Failed to send onboarding message: {e}")
            
            return {
                "status": "ok",
                "message_processed": False,
                "reason": "unlinked_number"
            }

        user_id = user.id

        # ========== PROCESS MESSAGE ==========
        response_data = await chat_service.process_message(
            db=db,
            message=message_content,
            user_id=user_id,
            channel="whatsapp",
        )

        ai_response = response_data.get("response", "Sorry, I couldn't process that.")

        # Send response (Issue #13: Now async)
        success = await whatsapp_service.send_message(phone_number, ai_response)

        if success:
            # ✅ FIXED Issue #18: Use PIIMasker for safe logging
            logger.info(
                "✅ Response sent to WhatsApp",
                extra={
                    "user_id": user_id,
                    "phone_masked": PIIMasker.mask_phone(phone_number),
                }
            )
        else:
            # ✅ FIXED Issue #18: Use PIIMasker for safe logging
            logger.error(
                "❌ Failed to send response to WhatsApp",
                extra={
                    "user_id": user_id,
                    "phone_masked": PIIMasker.mask_phone(phone_number),
                }
            )

        return {
            "status": "success",
            "message_processed": True,
            "user_id": user_id
        }

    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except ValueError as e:
        # Database constraint, validation error (not retryable)
        logger.warning(f"⚠️  Validation error: {e}")
        # Return 200 for non-retryable errors (Issue #17)
        return {
            "status": "ok",
            "message_processed": False,
            "reason": "validation_error"
        }
    except Exception as e:
        # Genuine internal failure (retryable)
        logger.exception("❌ WhatsApp webhook error (retryable)")
        # Let Meta know to retry rather than silently swallowing it as 200 (Issue #17)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service temporarily unavailable - message will be retried"
        ) from e


@router.get("/webhook/whatsapp")
async def verify_whatsapp_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    """
    Verify WhatsApp webhook with Meta.
    
    This endpoint is called by Meta during webhook setup to verify
    that the webhook URL is valid and belongs to the application.
    
    Args:
        hub_mode: Should be "subscribe"
        hub_challenge: Echo this back to Meta
        hub_verify_token: Must match WHATSAPP_VERIFY_TOKEN
        
    Returns:
        Challenge string if valid
        
    Raises:
        HTTPException(403): If verification token doesn't match
        HTTPException(500): If unexpected error
    """
    try:
        logger.info("🔐 WhatsApp webhook verification attempt")

        if hub_mode == "subscribe" and whatsapp_service.verify_webhook(hub_verify_token):
            logger.info("✅ WhatsApp webhook verified!")
            return PlainTextResponse(content=hub_challenge)
        else:
            logger.warning("❌ WhatsApp webhook verification failed (invalid token)")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid verification token"
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("❌ Webhook verification error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error verifying webhook"
        ) from e