"""WhatsApp Cloud API Integration Service"""
import requests
from typing import Optional
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

class WhatsAppService:
    """Handle WhatsApp messages via Meta Cloud API"""
    
    def __init__(self):
        self.access_token = settings.WHATSAPP_ACCESS_TOKEN
        self.phone_number_id = settings.WHATSAPP_PHONE_NUMBER_ID
        self.business_account_id = settings.WHATSAPP_BUSINESS_ACCOUNT_ID
        self.api_url = "https://graph.facebook.com/v18.0"
        self.verify_token = settings.WHATSAPP_VERIFY_TOKEN
    
    def verify_webhook(self, token: str) -> bool:
        """Verify webhook with WhatsApp"""
        return token == self.verify_token
    
    def send_message(self, phone_number: str, message: str) -> bool:
        """Send message to WhatsApp user"""
        try:
            url = f"{self.api_url}/{self.phone_number_id}/messages"
            
            payload = {
                "messaging_product": "whatsapp",
                "to": phone_number,
                "type": "text",
                "text": {"body": message}
            }
            
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json"
            }
            
            response = requests.post(url, json=payload, headers=headers)
            
            if response.status_code == 200:
                logger.info(f"✅ WhatsApp message sent to {phone_number}")
                return True
            else:
                logger.error(f"❌ Failed to send WhatsApp message: {response.text}")
                return False
        except Exception as e:
            logger.error(f"❌ WhatsApp send error: {str(e)}")
            return False
    
    def parse_webhook_message(self, body: dict) -> Optional[dict]:
        """Parse incoming WhatsApp webhook message"""
        try:
            entry = body.get("entry", [{}])[0]
            changes = entry.get("changes", [{}])[0]
            value = changes.get("value", {})
            
            messages = value.get("messages", [])
            if not messages:
                return None
            
            message = messages[0]
            phone_number = message.get("from")
            message_id = message.get("id")
            timestamp = message.get("timestamp")
            
            message_type = message.get("type")
            
            if message_type == "text":
                content = message.get("text", {}).get("body", "")
            elif message_type == "image":
                content = "[Image received]"
            elif message_type == "document":
                content = "[Document received]"
            elif message_type == "audio":
                content = "[Audio received]"
            else:
                content = "[Unsupported message type]"
            
            return {
                "phone_number": phone_number,
                "message_id": message_id,
                "timestamp": timestamp,
                "content": content,
                "type": message_type
            }
        except Exception as e:
            logger.error(f"Error parsing webhook: {str(e)}")
            return None
    
    def mark_as_read(self, message_id: str) -> bool:
        """Mark message as read on WhatsApp"""
        try:
            url = f"{self.api_url}/{self.phone_number_id}/messages"
            
            payload = {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": message_id
            }
            
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json"
            }
            
            response = requests.post(url, json=payload, headers=headers)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Error marking as read: {str(e)}")
            return False

whatsapp_service = WhatsAppService()