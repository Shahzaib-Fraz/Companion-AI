
import json
import logging
from fastapi import APIRouter, BackgroundTasks, Depends, Request, HTTPException
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.core.config import settings
from app.models.database import User
from app.services.whatsapp_service import whatsapp_service
from app.services.chat_service import chat_service
from app.schemas.schemas import PIIMasker

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/whatsapp")
def webhook_verify(request: Request):
    """
    WhatsApp webhook verification (GET).

    P0-9 FIX: now actually compares hub.verify_token to
    settings.WHATSAPP_VERIFY_TOKEN before echoing hub.challenge back,
    instead of accepting any request that merely included a challenge.
    """
    verify_token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if not settings.WHATSAPP_VERIFY_TOKEN:
        logger.error("WHATSAPP_VERIFY_TOKEN is not configured - refusing to verify")
        raise HTTPException(status_code=403, detail="Webhook verification not configured")

    if challenge and verify_token == settings.WHATSAPP_VERIFY_TOKEN:
        logger.info("✅ WhatsApp webhook verified")
        return int(challenge)

    logger.warning("❌ WhatsApp webhook verification failed (missing/incorrect verify_token)")
    raise HTTPException(status_code=403, detail="Webhook verification failed")


async def _process_message_background(provider_message_id: str, user_id: int, phone: str, text: str):
    """
    Runs as a FastAPI BackgroundTask, AFTER the webhook has already
    returned 200 to Meta - see the P0-8 note at the top of this file for
    what this does and does not guarantee.

    Uses its own DB session (the request's `db` dependency is torn down
    once the response is sent, so it can't safely be reused here).

    Assumes app/db/database.py exposes a `SessionLocal` sessionmaker
    alongside `get_db` (the standard FastAPI pattern) - adjust this
    import if your module names it differently.
    """
    from app.db.database import SessionLocal

    db = SessionLocal()
    try:
        try:
            await whatsapp_service.mark_as_read(provider_message_id)
        except Exception as e:
            logger.warning(f"mark_as_read failed for {provider_message_id}: {e}")

        try:
            response = await chat_service.process_message(
                db=db, message=text, user_id=user_id, channel="whatsapp"
            )
            ai_response = response.get("response")
        except Exception as e:
            logger.exception(f"chat_service failed for {provider_message_id}: {e}")
            ai_response = "Sorry, there was an error processing your message. Please try again."

        if ai_response:
            sent = await whatsapp_service.send_message(phone, ai_response)
            if sent:
                logger.info(f"✅ Sent AI response to {PIIMasker.mask_phone(phone)}")
            else:
                logger.error(f"❌ Failed to send response to {PIIMasker.mask_phone(phone)}")
        else:
            logger.error(f"No response generated for {provider_message_id}")

    finally:
        db.close()


@router.post("/whatsapp")
async def webhook(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    WhatsApp webhook.

    1. Verify the HMAC signature (P0-9).
    2. Parse ALL messages (P0-7).
    3. Persist the event, then persist+dedupe each message id (P0-6,
       and the P0-8 event-level race fix in whatsapp_service - see
       persist_webhook_event) - fast, synchronous, durable.
    4. Return 200 immediately; hand the actual AI processing + reply to
       a background task (P0-8) so a slow LLM call can't blow Meta's
       webhook timeout.
    """
    raw_body = await request.body()

    signature = request.headers.get("X-Hub-Signature-256", "")
    if not whatsapp_service.verify_webhook_signature(raw_body.decode("utf-8"), signature):
        logger.warning("❌ WhatsApp webhook signature verification failed")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(raw_body)
    except Exception as e:
        logger.error(f"Failed to parse webhook: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.info("📨 WhatsApp webhook received")

    messages = whatsapp_service.parse_all_webhook_messages(payload)
    if not messages:
        logger.info("No messages to process")
        return {"status": "ok", "processed": 0}

    logger.info(f"🔄 Persisting {len(messages)} message(s)")

    # P0-8 (event race) FIX: persist_webhook_event is now an atomic
    # get-or-create (see whatsapp_service.py) - no separate
    # SELECT-before-insert pre-check needed here anymore.
    provider_event_id = payload.get("entry", [{}])[0].get("id", "unknown")
    webhook_event_id = await whatsapp_service.persist_webhook_event(db, payload, provider_event_id)
    if not webhook_event_id:
        db.rollback()
        return {"status": "error", "detail": "Failed to persist webhook"}

    queued_count = 0
    errors = []

    for msg in messages:
        try:
            phone = f"+{msg['from']}"  # Normalize with +
            text = msg["content"]
            provider_message_id = msg["provider_message_id"]

            logger.info(f"📱 Persisting message from {PIIMasker.mask_phone(phone)}")

            user = db.query(User).filter(User.phone_number == phone).first()

            if not user:
                logger.warning(f"❌ User not found for phone: {PIIMasker.mask_phone(phone)}")
                background_tasks.add_task(
                    whatsapp_service.send_message,
                    phone,
                    "Sorry, we couldn't find your account. Please check your phone number.",
                )
                errors.append(f"No user for {PIIMasker.mask_phone(phone)}")
                continue

            # P0-6: idempotency - persist BEFORE any processing
            persisted = await whatsapp_service.persist_webhook_message(
                db, webhook_event_id, user.id, provider_message_id
            )

            if not persisted:
                logger.info(f"⏭️  Message already processed: {provider_message_id}")
                continue

            # P0-8: hand off the slow part (mark-as-read, chat LLM call,
            # outbound send) to a background task instead of awaiting it
            # here before responding to Meta.
            background_tasks.add_task(
                _process_message_background, provider_message_id, user.id, phone, text
            )
            queued_count += 1

        except Exception as e:
            logger.exception(f"Error persisting message: {e}")
            errors.append(str(e))

    try:
        db.commit()
        logger.info(f"✅ Persisted+queued {queued_count}/{len(messages)} messages")
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to commit: {e}")
        return {"status": "error", "detail": "Failed to commit changes"}

    return {
        "status": "ok",
        "queued": queued_count,
        "total": len(messages),
        "errors": errors if errors else None,
    }


@router.get("/whatsapp/health")
def whatsapp_health():
    """Health check"""
    return {"status": "ok", "service": "whatsapp"}