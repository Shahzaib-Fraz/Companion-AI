import html
import logging
import os

import requests

from app.core.config import settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15
BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def _setting(name: str):
    """settings first, then the raw environment."""
    value = getattr(settings, name, None)
    if value in (None, ""):
        value = os.getenv(name)
    return value or None


class EmailService:
    def __init__(self):
        self.api_key = _setting("BREVO_API_KEY")
        self.sender = _setting("BREVO_SENDER_EMAIL")
        self.sender_name = _setting("BREVO_SENDER_NAME") or "Your AI Companion"

        if not self.api_key or not self.sender:
            logger.error(
                "Brevo is not configured (api_key=%s, sender=%s); reminders cannot be sent",
                bool(self.api_key), self.sender,
            )
        else:
            logger.info("Email mode: Brevo | from %s", self.sender)

    def send_reminder(self, user_email: str, content: str) -> bool:
        """True only if Brevo accepted the message. Never guess."""
        if not self.api_key or not self.sender:
            logger.error("Brevo is not configured; cannot send to %s", user_email)
            return False

        safe = html.escape(content or "")
        payload = {
            "sender": {"email": self.sender, "name": self.sender_name},
            "to": [{"email": user_email}],
            "subject": "Reminder",
            "textContent": content or "",
            "htmlContent": (
                "<div style=\"font-family:system-ui,Segoe UI,Arial,sans-serif;"
                "font-size:16px;line-height:1.5;color:#111\">"
                "<p style=\"margin:0 0 12px\">Here's your reminder:</p>"
                f"<p style=\"margin:0;font-weight:600\">{safe}</p>"
                "</div>"
            ),
        }
        headers = {
            "api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            response = requests.post(
                BREVO_URL, json=payload, headers=headers, timeout=TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            logger.error("Email transport error for %s: %s", user_email, exc)
            return False

        if response.status_code == 401:
            logger.error(
                "Brevo 401 Unauthorized: check API key. Body: %s", response.text[:300]
            )
            return False

        if response.status_code == 400:
            logger.error(
                "Brevo 400 Bad Request for %s — likely sender email %s is not "
                "verified yet. Body: %s",
                user_email, self.sender, response.text[:400],
            )
            return False

        if not (200 <= response.status_code < 300):
            logger.error(
                "Brevo rejected the send to %s (%s): %s",
                user_email, response.status_code, response.text[:500],
            )
            return False

        logger.info("Reminder email accepted for %s", user_email)
        return True


email_service = EmailService()