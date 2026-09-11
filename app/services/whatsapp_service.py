"""WhatsApp Cloud API Integration Service - Async with Timeout & Deduplication"""
import httpx
import logging
import os
from typing import Optional, List
from app.core.config import settings

logger = logging.getLogger(__name__)


def _setting(name: str):
    """Settings first, then raw environment variable."""
    value = getattr(settings, name, None)
    if value in (None, ""):
        value = os.getenv(name)
    return value or None


# Configure timeout for all WhatsApp API calls (Issue #14: No Timeout)
WHATSAPP_TIMEOUT = httpx.Timeout(
    timeout=30.0,      # Total request timeout
    connect=10.0,      # Connection timeout
    read=20.0,         # Read timeout
    write=10.0,        # Write timeout
    pool=10.0          # Pool timeout
)

# Create async HTTP client with timeout (Issue #13: Blocking Calls)
_client: Optional[httpx.AsyncClient] = None


async def get_async_client() -> httpx.AsyncClient:
    """Get or create async HTTP client"""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=WHATSAPP_TIMEOUT)
    return _client


async def close_async_client():
    """Close async client (call from app shutdown)"""
    global _client
    if _client:
        await _client.aclose()
        _client = None


class WhatsAppService:
    """Handle WhatsApp messages via Meta Cloud API"""
    
    def __init__(self):
        # FIXED: Use _setting() to read from settings OR .env file
        self.access_token = _setting("WHATSAPP_ACCESS_TOKEN")
        self.phone_number_id = _setting("WHATSAPP_PHONE_NUMBER_ID")
        self.business_account_id = _setting("WHATSAPP_BUSINESS_ACCOUNT_ID")
        self.api_url = "https://graph.facebook.com/v18.0"
        self.verify_token = _setting("WHATSAPP_VERIFY_TOKEN")
        
        if not self.access_token or not self.phone_number_id:
            logger.error(
                "WhatsApp not configured (token=%s, phone_id=%s); cannot send messages",
                bool(self.access_token),
                bool(self.phone_number_id),
            )
        else:
            logger.info("✅ WhatsApp configured | phone_id=%s", self.phone_number_id)
    
    def verify_webhook(self, token: str) -> bool:
        """Verify webhook with WhatsApp"""
        return token == self.verify_token
    
    async def send_message(self, phone_number: str, message: str) -> bool:
        """
        Send message to WhatsApp user.
        
        FIXED (Issue #13): Now uses async httpx instead of blocking requests
        FIXED (Issue #14): Includes timeout configuration
        FIXED: Now reads token from .env via _setting()
        
        Args:
            phone_number: Recipient phone number
            message: Message text
            
        Returns:
            True if successful, False otherwise
        """
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
            
            # FIXED: Use async client with timeout (Issue #13, #14)
            client = await get_async_client()
            response = await client.post(
                url,
                json=payload,
                headers=headers,
                timeout=WHATSAPP_TIMEOUT
            )
            
            if response.status_code == 200:
                logger.info(f"✅ WhatsApp message sent to {phone_number}")
                return True
            else:
                logger.error(f"❌ Failed to send WhatsApp message: {response.text}")
                return False
                
        except httpx.TimeoutException:
            logger.error(f"❌ WhatsApp request timeout sending to {phone_number}")
            return False
        except Exception as e:
            logger.error(f"❌ WhatsApp send error: {str(e)}")
            return False
    
    def parse_webhook_message(self, body: dict) -> Optional[dict]:
        """
        Parse FIRST incoming WhatsApp webhook message.
        
        Note: For processing ALL messages in webhook, use parse_all_webhook_messages()
        
        Args:
            body: Webhook payload
            
        Returns:
            First message dict or None
        """
        try:
            # Get first message (backward compatible)
            messages = self._extract_all_messages(body)
            if messages:
                return messages[0]
            return None
        except Exception as e:
            logger.error(f"Error parsing webhook: {str(e)}")
            return None
    
    def _extract_all_messages(self, body: dict) -> List[dict]:
        """
        Extract ALL messages from webhook payload.
        
        FIXED (Issue #16): Now iterates through all entries, changes, and messages
        instead of just taking the first one.
        
        Args:
            body: Webhook payload
            
        Returns:
            List of parsed messages
        """
        messages = []
        
        try:
            # Iterate through all entries (Issue #16: was entry[0])
            for entry in body.get("entry", []):
                # Iterate through all changes (Issue #16: was changes[0])
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    
                    # Iterate through all messages (Issue #16: was messages[0])
                    for message in value.get("messages", []):
                        try:
                            parsed = self._parse_single_message(message)
                            if parsed:
                                messages.append(parsed)
                        except Exception as e:
                            logger.warning(f"Error parsing individual message: {e}")
                            continue
        
        except Exception as e:
            logger.error(f"Error extracting messages from webhook: {e}")
        
        return messages
    
    def _parse_single_message(self, message: dict) -> Optional[dict]:
        """
        Parse a single message from webhook.
        
        Args:
            message: Message object from webhook
            
        Returns:
            Parsed message dict
        """
        phone_number = message.get("from")
        message_id = message.get("id")
        timestamp = message.get("timestamp")
        message_type = message.get("type")
        
        # Extract content based on type
        if message_type == "text":
            content = message.get("text", {}).get("body", "")
        elif message_type == "image":
            content = "[Image received]"
        elif message_type == "document":
            content = "[Document received]"
        elif message_type == "audio":
            content = "[Audio received]"
        elif message_type == "video":
            content = "[Video received]"
        else:
            logger.warning(f"Unsupported message type: {message_type}")
            content = "[Unsupported message type]"
        
        return {
            "phone_number": phone_number,
            "message_id": message_id,
            "timestamp": timestamp,
            "content": content,
            "type": message_type
        }
    
    async def mark_as_read(self, message_id: str) -> bool:
        """
        Mark message as read on WhatsApp.
        
        FIXED (Issue #13): Now uses async httpx instead of blocking requests
        FIXED (Issue #14): Includes timeout configuration
        
        Args:
            message_id: WhatsApp message ID
            
        Returns:
            True if successful, False otherwise
        """
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
            
            # FIXED: Use async client with timeout (Issue #13, #14)
            client = await get_async_client()
            response = await client.post(
                url,
                json=payload,
                headers=headers,
                timeout=WHATSAPP_TIMEOUT
            )
            
            if response.status_code == 200:
                logger.debug(f"✅ Message {message_id} marked as read")
                return True
            else:
                logger.warning(f"Failed to mark message as read: {response.text}")
                return False
                
        except httpx.TimeoutException:
            logger.error(f"Timeout marking message as read: {message_id}")
            return False
        except Exception as e:
            logger.error(f"Error marking as read: {str(e)}")
            return False


whatsapp_service = WhatsAppService()