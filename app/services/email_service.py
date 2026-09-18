
import html as html_lib
import logging
from typing import Optional

import httpx

from app.core.config import settings
from app.schemas.schemas import PIIMasker

logger = logging.getLogger(__name__)

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"


class EmailService:
    """Email service for sending reminders and notifications via Brevo's transactional email API."""

    def __init__(self):
        self.sender_email = settings.BREVO_SENDER_EMAIL
        self.sender_name = settings.BREVO_SENDER_NAME
        self.api_key = settings.BREVO_API_KEY

    async def _send_via_brevo(
        self,
        to_email: str,
        to_name: Optional[str],
        subject: str,
        html_content: str,
        text_content: str,
    ) -> bool:
        """
        The actual API call - both send_reminder() and send_notification()
        below route through this single place, so there's one spot that
        owns auth, the request shape, and error logging.
        """
        if not self.api_key:
            logger.error("BREVO_API_KEY is not configured - cannot send email")
            return False

        if not self.sender_email:
            logger.error("BREVO_SENDER_EMAIL is not configured - cannot send email")
            return False

        recipient: dict = {"email": to_email}
        if to_name:
            recipient["name"] = to_name

        payload = {
            "sender": {"name": self.sender_name, "email": self.sender_email},
            "to": [recipient],
            "subject": subject,
            "htmlContent": html_content,
            "textContent": text_content,
        }
        headers = {
            "accept": "application/json",
            "api-key": self.api_key,
            "content-type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(BREVO_API_URL, json=payload, headers=headers)

            if response.status_code in (200, 201):
                message_id = None
                try:
                    message_id = response.json().get("messageId")
                except Exception:
                    pass
                logger.info(
                    "✅ Email sent via Brevo",
                    extra={
                        "email_masked": PIIMasker.mask_email(to_email),
                        "message_id": message_id,
                    },
                )
                return True

            # This is the log line the old stub could never produce,
            # because it never made a real call that could fail. If
            # something's wrong on Brevo's side (unverified sender,
            # blocked domain, bad template, rate limit), the actual
            # reason shows up here now.
            logger.error(
                "❌ Brevo send failed",
                extra={
                    "email_masked": PIIMasker.mask_email(to_email),
                    "status_code": response.status_code,
                    "response_body": response.text[:500],
                },
            )
            return False

        except Exception as e:
            logger.error(f"❌ Brevo API call failed: {e}")
            return False

    async def send_reminder(
        self,
        user_email: str,
        content: str,
        user_name: Optional[str] = None,
    ) -> bool:
        """
        Send reminder email.

        Args:
            user_email: Recipient email
            content: Reminder content
            user_name: Optional user name for personalization

        Returns:
            True if Brevo accepted the send, False otherwise
        """
        if not user_email:
            logger.warning("No email address provided")
            return False

        greeting = user_name or "there"
        # content is user-supplied (the reminder text itself) - escape
        # it before embedding in HTML so it can't break the email's
        # markup or inject anything into the rendered message.
        safe_content = html_lib.escape(content or "")
        safe_greeting = html_lib.escape(greeting)

        subject = "⏰ Your Reminder"
        html_content = (
            "<html><body>"
            f"<p>Hello {safe_greeting},</p>"
            "<p>You have a reminder:</p>"
            f"<p><strong>{safe_content}</strong></p>"
            "<p>---<br>AI Companion</p>"
            "</body></html>"
        )
        text_content = (
            f"Hello {greeting},\n\n"
            f"You have a reminder:\n\n{content}\n\n"
            "---\nAI Companion"
        )

        return await self._send_via_brevo(user_email, user_name, subject, html_content, text_content)

    async def send_notification(
        self,
        user_email: str,
        subject: str,
        message: str,
    ) -> bool:
        """Send general notification email"""
        if not user_email:
            logger.warning("No email address provided")
            return False

        safe_message = html_lib.escape(message or "")
        html_content = f"<html><body><p>{safe_message}</p></body></html>"

        return await self._send_via_brevo(user_email, None, subject, html_content, message)


# Global instance
email_service = EmailService()