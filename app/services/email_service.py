import html
import logging
import os

import requests

from app.core.config import settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15
BREVO_URL = "https://api.brevo.com/v3/smtp/email"
SUBJECT_MAX_LEN = 60


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

    def send_reminder(self, user_email: str, content: str, user_name: str | None = None) -> bool:
        """True only if Brevo accepted the message. Never guess.

        FIXED (Issue #42: Reminder emails looked like a bare content dump
        instead of a real notification): subject now reflects the actual
        reminder instead of a static "Reminder", and the body reads as a
        short, warm note rather than a label + raw string.

        Args:
            user_email: Recipient's email address
            content: The short imperative reminder text (e.g. "Go for a walk")
            user_name: Optional display name for a personalised greeting
        """
        if not self.api_key or not self.sender:
            logger.error("Brevo is not configured; cannot send to %s", user_email)
            return False

        content = (content or "").strip()
        safe = html.escape(content)
        first_name = (user_name or "").strip().split(" ")[0] if user_name else ""
        greeting = f"Hi {html.escape(first_name)}," if first_name else "Hi there,"
        subject = self._subject_for(content)

        text_content = (
            f"{('Hi ' + first_name) if first_name else 'Hi there'},\n\n"
            f"Just a friendly reminder: {content}\n\n"
            f"— {self.sender_name}"
        )

        html_content = (
            "<div style=\"font-family:system-ui,Segoe UI,Arial,sans-serif;"
            "font-size:16px;line-height:1.6;color:#111;max-width:480px;margin:0 auto\">"
            f"<p style=\"margin:0 0 14px\">{greeting}</p>"
            "<p style=\"margin:0 0 16px\">Just a friendly reminder to:</p>"
            "<p style=\"margin:0 0 22px;padding:12px 16px;background:#f4f6f8;"
            "border-left:4px solid #4f46e5;border-radius:6px;font-weight:600\">"
            f"{safe}</p>"
            f"<p style=\"margin:0;color:#666;font-size:14px\">— {html.escape(self.sender_name)} 💙</p>"
            "</div>"
        )

        payload = {
            "sender": {"email": self.sender, "name": self.sender_name},
            "to": [{"email": user_email}],
            "subject": subject,
            "textContent": text_content,
            "htmlContent": html_content,
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

    @staticmethod
    def _subject_for(content: str) -> str:
        text = (content or "").strip()
        if not text:
            return "⏰ Reminder"
        if len(text) > SUBJECT_MAX_LEN:
            text = text[: SUBJECT_MAX_LEN - 1].rstrip() + "…"
        return f"⏰ Reminder: {text}"


email_service = EmailService()