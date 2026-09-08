"""WhatsApp Webhook Routes"""
from fastapi import APIRouter, Request, Query, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.services.chat_service import chat_service
from app.services.whatsapp_service import whatsapp_service
from app.repositories.user_repository import UserRepository
import logging

logger = logging.getLogger(__name__)

router = APIRouter()

@router.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request, db: Session = Depends(get_db)):
    """Receive messages from WhatsApp Cloud API"""
    try:
        body = await request.json()
        logger.info(f"📱 WhatsApp webhook received")
        
        # Parse incoming message
        parsed_message = whatsapp_service.parse_webhook_message(body)
        
        if not parsed_message:
            logger.warning("No valid message in webhook")
            return {"status": "ok"}
        
        phone_number = parsed_message["phone_number"]
        message_content = parsed_message["content"]
        message_id = parsed_message["message_id"]
        
        logger.info(f"📱 Message from {phone_number}: {message_content}")
        
        # Mark as read on WhatsApp
        whatsapp_service.mark_as_read(message_id)
        
        # Get or create user by phone number
        user = UserRepository.get_by_phone(db, phone_number)
        
        if not user:
            logger.info(f"👤 New WhatsApp user: {phone_number}")
            user = UserRepository.create(db, email=f"{phone_number}@whatsapp.local", phone_number=phone_number)
        
        user_id = user.id
        
        # Process message through chat service
        response_data = await chat_service.process_message(
            db=db,
            message=message_content,
            user_id=user_id,
            channel="whatsapp"
        )
        
        # Get the AI response
        ai_response = response_data.get("response", "Sorry, I couldn't process that.")
        
        # Send response back to WhatsApp
        success = whatsapp_service.send_message(phone_number, ai_response)
        
        if success:
            logger.info(f"✅ Response sent to WhatsApp: {phone_number}")
        else:
            logger.error(f"❌ Failed to send response to WhatsApp")
        
        return {"status": "success", "message_processed": True}
        
    except Exception as e:
        logger.error(f"❌ WhatsApp webhook error: {str(e)}")
        return {"status": "error", "message": str(e)}


@router.get("/webhook/whatsapp")
async def verify_whatsapp_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    """Verify WhatsApp webhook with Meta"""
    try:
        logger.info(f"🔐 WhatsApp webhook verification attempt")
        
        if hub_mode == "subscribe" and whatsapp_service.verify_webhook(hub_verify_token):
            logger.info("✅ WhatsApp webhook verified!")
            return PlainTextResponse(content=hub_challenge)
        else:
            logger.warning("❌ WhatsApp webhook verification failed")
            return {"error": "Invalid verification token"}
    except Exception as e:
        logger.error(f"❌ Webhook verification error: {str(e)}")
        return {"error": str(e)}