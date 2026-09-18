

import hmac
import hashlib
from app.schemas.schemas import PIIMasker
import httpx
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime
from app.core.config import settings

logger = logging.getLogger(__name__)


class WhatsAppService:
    """WhatsApp Cloud API integration with idempotency and templates"""

    def __init__(self):
        self.access_token = settings.WHATSAPP_ACCESS_TOKEN
        self.phone_number_id = settings.WHATSAPP_PHONE_NUMBER_ID
        self.app_secret = settings.WHATSAPP_APP_SECRET
        self.api_url = "https://graph.facebook.com/v18.0"

        # P0-9: Validate app secret at initialization
        if not self.app_secret or self.app_secret in ["", "placeholder"]:
            raise ValueError("WHATSAPP_APP_SECRET cannot be empty (security risk)")

    def verify_webhook_signature(self, request_body: str, signature: str) -> bool:
        """
        P0-9: Verify webhook signature with HMAC-SHA256
        Constant-time comparison to prevent timing attacks.

        NOTE: this method existed before, but nothing in whatsapp_routes.py
        actually called it - see the updated whatsapp_routes.py, which now
        calls this on every incoming POST before doing anything else.
        """
        if not self.app_secret:
            logger.warning("Cannot verify webhook: app_secret not configured")
            return False

        if not signature:
            return False

        expected = hmac.new(
            self.app_secret.encode(),
            request_body.encode(),
            hashlib.sha256,
        ).hexdigest()

        # Constant-time comparison
        return hmac.compare_digest(
            f"sha256={expected}",
            signature,
        )

    def parse_all_webhook_messages(self, body: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        P0-7 FIX: Parse and return ALL messages from webhook payload.

        Meta sends multiple messages in one webhook. Previous code only processed first.
        Now processes all messages in all entries/changes.
        """
        messages = []

        try:
            for entry in body.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})

                    # Extract ALL messages (not just first)
                    for message in value.get("messages", []):
                        parsed = self._parse_single_message(message)
                        if parsed:
                            messages.append(parsed)
                            logger.info(f"Parsed message: {parsed['provider_message_id']}")

        except Exception as e:
            logger.error(f"Error parsing webhook messages: {e}")

        logger.info(f"Total messages parsed: {len(messages)}")
        return messages

    def _parse_single_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Parse a single message from webhook"""
        try:
            message_id = message.get("id")
            from_number = message.get("from")
            msg_type = message.get("type")
            timestamp = message.get("timestamp")

            if not message_id or not from_number:
                return None

            # Extract content based on type
            if msg_type == "text":
                content = message.get("text", {}).get("body", "")
            elif msg_type == "image":
                content = "[Image]"
            elif msg_type == "document":
                content = "[Document]"
            elif msg_type == "audio":
                content = "[Audio]"
            elif msg_type == "video":
                content = "[Video]"
            elif msg_type == "sticker":
                content = "[Sticker]"
            else:
                logger.warning(f"Unsupported message type: {msg_type}")
                return None

            return {
                "provider_message_id": message_id,
                "from": from_number,
                "type": msg_type,
                "content": content,
                "timestamp": timestamp,
            }

        except Exception as e:
            logger.error(f"Error parsing message: {e}")
            return None

    async def persist_webhook_event(self, db, payload: Dict[str, Any], provider_event_id: str) -> Optional[int]:
        """
        P0-8 FIX: Durably persist webhook event BEFORE processing.
        If app crashes during processing, event can be recovered.

        P0-8 (event race) FIX: this used to be a plain insert, and the
        route did its own SELECT-before-insert to avoid duplicate events -
        exactly the race-prone pattern P0-6 flagged at the message level.
        Now it's a real atomic get-or-create: on a unique-constraint
        conflict (two concurrent deliveries of the same event), this
        looks up and returns the row the other request already created
        instead of treating it as a failure. The route no longer needs
        its own pre-check.
        """
        from app.models.database import WebhookEvent
        from sqlalchemy.exc import IntegrityError

        try:
            event = WebhookEvent(
                provider="whatsapp",
                provider_event_id=provider_event_id,
                payload=payload,
                status="RECEIVED",
            )
            db.add(event)
            db.flush()

            logger.info(f"✅ Persisted webhook event: {provider_event_id}")
            return event.id

        except IntegrityError:
            db.rollback()
            existing = db.query(WebhookEvent).filter(
                WebhookEvent.provider_event_id == provider_event_id
            ).first()
            if existing:
                logger.info(f"♻️  Webhook event already persisted: {provider_event_id}")
                return existing.id
            # Unique-constraint conflict but the row isn't there on lookup
            # (e.g. concurrent delete) - genuinely unexpected, don't loop.
            logger.error(f"Webhook event conflict but not found on lookup: {provider_event_id}")
            return None

        except Exception as e:
            logger.error(f"Failed to persist webhook: {e}")
            db.rollback()
            return None

    async def persist_webhook_message(
        self,
        db,
        webhook_event_id: int,
        user_id: int,
        provider_message_id: str
    ) -> bool:
        """
        P0-6 FIX: Durably store external_message_id BEFORE processing.
        Prevents duplicate processing if unique constraint is hit.
        """
        from app.models.database import WebhookMessage
        from sqlalchemy.exc import IntegrityError

        try:
            webhook_msg = WebhookMessage(
                webhook_event_id=webhook_event_id,
                user_id=user_id,
                provider_message_id=provider_message_id,
                status="RECEIVED",
            )
            db.add(webhook_msg)
            db.flush()

            logger.info(f"✅ Persisted webhook message: {provider_message_id}")
            return True

        except IntegrityError:
            # P0-6: Message already processed (idempotent)
            logger.info(f"♻️  Message already processed: {provider_message_id}")
            db.rollback()
            return False

        except Exception as e:
            logger.error(f"Failed to persist webhook message: {e}")
            db.rollback()
            return False

    async def send_message(self, phone_number: str, message_text: str) -> bool:
        """Send free-form message (for conversation replies)"""
        url = f"{self.api_url}/{self.phone_number_id}/messages"

        payload = {
            "messaging_product": "whatsapp",
            "to": phone_number,
            "type": "text",
            "text": {"body": message_text}
        }

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(url, json=payload, headers=headers)

            if response.status_code == 200:
                logger.info(f"✅ Message sent to {PIIMasker.mask_phone(phone_number)}")
                return True
            else:
                logger.error(f"WhatsApp error: {response.status_code}")
                return False

        except Exception as e:
            logger.error(f"WhatsApp send failed: {e}")
            return False

    async def send_template_message(
        self,
        phone_number: str,
        template_name: str,
        language: str = "en_US",
        parameters: List[str] = None
    ) -> bool:
        """
        P0-10 FIX: Send template message (for scheduled/proactive notifications)

        Uses WhatsApp's approved templates instead of free-form text for business-initiated
        messages outside customer-service window.
        """
        url = f"{self.api_url}/{self.phone_number_id}/messages"

        payload = {
            "messaging_product": "whatsapp",
            "to": phone_number,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {
                    "code": language,
                },
            }
        }

        # Add parameters if provided - Meta requires them nested inside a
        # "components" array on the template object, not a top-level
        # "parameters" key (Meta rejects the flatter shape with
        # "Unexpected key \"parameters\" on param \"template\"" - this was
        # a real bug in my original version, fixed independently and
        # confirmed against Meta's actual API contract).
        if parameters:
            payload["template"]["components"] = [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": str(p)} for p in parameters
                    ],
                }
            ]

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(url, json=payload, headers=headers)

            if response.status_code == 200:
                result = response.json()
                msg_id = result.get('messages', [{}])[0].get('id')
                logger.info(f"✅ Template message sent: {template_name} to {PIIMasker.mask_phone(phone_number)} (ID: {msg_id})")
                return True
            else:
                logger.error(f"Template send failed: {response.status_code} - {response.text[:200]}")
                return False

        except Exception as e:
            logger.error(f"Template send error: {e}")
            return False

    async def mark_as_read(self, message_id: str) -> bool:
        """Mark message as read"""
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

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(url, json=payload, headers=headers)

            return response.status_code == 200

        except Exception as e:
            logger.error(f"Mark as read failed: {e}")
            return False


# Global instance
whatsapp_service = WhatsAppService()


async def send_reminder_whatsapp(phone_number: str, content: str, user_name: str = None) -> bool:
    """Send reminder via WhatsApp template API"""
    try:
        return await whatsapp_service.send_template_message(
            phone_number=phone_number,
            template_name="reminder_notification",
            parameters=[content[:1000] if content else ""]
        )
    except Exception as e:
        logger.error(f"Reminder send failed: {e}")
        return False