"""
Scheduler Service - FIXED to mark reminders as sent
CRITICAL FIX: Sets sent_at timestamp after sending to prevent duplicate sends
"""
import asyncio
import logging
from datetime import datetime
from sqlalchemy.orm import Session
from app.models.database import Reminder, User
from app.services.email_service import email_service
from app.services.whatsapp_service import whatsapp_service

logger = logging.getLogger(__name__)


class ReminderSchedulerService:
    """Service for scheduling and dispatching reminders via multiple channels"""

    def __init__(self, db: Session):
        self.db = db
        self._whatsapp_service = whatsapp_service
        self._email_service = email_service

    async def send_reminders(self):
        """
        Main scheduler job to find and send due reminders.
        
        CRITICAL FIXED: Now marks reminders as sent after sending
        so they don't send again and again
        
        This is called by APScheduler every 1-5 minutes.
        Only processes reminders that:
        1. Are scheduled for past time (scheduled_at <= now)
        2. Haven't been sent yet (sent_at is NULL)
        """
        try:
            logger.info("Starting reminder check...")
            
            # Find ALL UNSENT reminders that are past their scheduled time
            now = datetime.utcnow()
            
            unsent_reminders = self.db.query(Reminder).filter(
                Reminder.scheduled_at <= now,
                Reminder.sent_at == None  # CRITICAL: Only unsent reminders!
            ).all()
            
            if not unsent_reminders:
                logger.info("No reminders to send")
                return
            
            logger.info(f"Found {len(unsent_reminders)} reminders to send")
            
            # Process each reminder
            for reminder in unsent_reminders:
                try:
                    user = self.db.query(User).filter(
                        User.id == reminder.user_id
                    ).first()
                    
                    if not user:
                        logger.warning(f"User {reminder.user_id} not found for reminder {reminder.id}")
                        # Mark as sent anyway so we don't keep trying
                        reminder.sent_at = datetime.utcnow()
                        self.db.commit()
                        continue
                    
                    # Send via all configured channels
                    channels_sent = []
                    send_success = False
                    
                    # Try email
                    if user.email:
                        try:
                            email_result = await self._dispatch_email(user.email, reminder.content)
                            if email_result:
                                channels_sent.append("email")
                                send_success = True
                        except Exception as e:
                            logger.error(
                                f"Email dispatch failed for reminder {reminder.id}",
                                extra={
                                    "reminder_id": reminder.id,
                                    "user_id": reminder.user_id,
                                    "error": str(e),
                                }
                            )
                    
                    # Try WhatsApp
                    if user.phone_number:
                        try:
                            whatsapp_result = await self._dispatch_whatsapp(user.phone_number, reminder.content)
                            if whatsapp_result:
                                channels_sent.append("whatsapp")
                                send_success = True
                        except Exception as e:
                            logger.error(
                                f"WhatsApp dispatch failed for reminder {reminder.id}",
                                extra={
                                    "reminder_id": reminder.id,
                                    "user_id": reminder.user_id,
                                    "error": str(e),
                                }
                            )
                    
                    # CRITICAL FIX: Mark as sent ONLY AFTER attempts (success or failure)
                    # This prevents the reminder from being sent again next scheduler run
                    reminder.sent_at = datetime.utcnow()
                    self.db.commit()
                    
                    logger.info(
                        f"Reminder {reminder.id} processed",
                        extra={
                            "reminder_id": reminder.id,
                            "user_id": reminder.user_id,
                            "channels": "+".join(channels_sent) if channels_sent else "none",
                            "success": send_success,
                        }
                    )
                
                except Exception as e:
                    logger.exception(
                        f"Error processing reminder {reminder.id}",
                        extra={
                            "reminder_id": reminder.id,
                            "user_id": reminder.user_id,
                            "error": str(e),
                        }
                    )
                    # Still mark as sent so we don't retry forever
                    try:
                        reminder.sent_at = datetime.utcnow()
                        self.db.commit()
                    except Exception as commit_error:
                        logger.error(f"Failed to mark reminder as sent: {commit_error}")
        
        except Exception as e:
            logger.exception(
                "Fatal error in reminder scheduler",
                extra={"error": str(e)}
            )

    async def _dispatch_email(self, email: str, content: str) -> bool:
        """
        Dispatch reminder via email.
        
        Args:
            email: Email address
            content: Reminder content
            
        Returns:
            True if sent successfully, False otherwise
        """
        try:
            result = self._email_service.send_reminder(
                user_email=email,
                content=content
            )
            
            logger.info(
                "Reminder email sent",
                extra={
                    "email": email[:3] + "***",
                    "status": "accepted" if result else "rejected",
                }
            )
            return result
        
        except Exception as e:
            logger.error(
                "Email dispatch error",
                extra={
                    "email": email[:3] + "***",
                    "error": str(e),
                }
            )
            return False

    async def _dispatch_whatsapp(self, phone: str, content: str) -> bool:
        """
        Dispatch reminder via WhatsApp.
        
        Args:
            phone: Phone number
            content: Reminder content
            
        Returns:
            True if sent successfully, False otherwise
        """
        try:
            await self._whatsapp_service.send_message(phone, content)
            
            logger.info(
                "Reminder WhatsApp sent",
                extra={
                    "phone": phone[:4] + "****" + phone[-2:],
                    "status": "sent",
                }
            )
            return True
        
        except Exception as e:
            logger.error(
                "WhatsApp dispatch error",
                extra={
                    "phone": phone[:4] + "****" + phone[-2:],
                    "error": str(e),
                }
            )
            return False


# Singleton instance
_reminder_scheduler_service = None


def get_reminder_scheduler_service(db: Session) -> ReminderSchedulerService:
    """Get or create reminder scheduler service"""
    global _reminder_scheduler_service
    if _reminder_scheduler_service is None:
        _reminder_scheduler_service = ReminderSchedulerService(db)
    return _reminder_scheduler_service