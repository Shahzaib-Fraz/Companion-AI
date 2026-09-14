"""
Scheduler Service - FIXED to work with your actual structure
PLUS: Message Summarization (every 6 hours)
Combines reminder dispatch + conversation summaries in one scheduler
"""
import asyncio
import logging
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.models.database import Reminder, User
from app.services.email_service import email_service
from app.services.whatsapp_service import whatsapp_service
from app.repositories.message_repository import MessageRepository
from app.services.embedding_service import embedding_service
from app.services.memory_service import memory_service
from app.services.llm_service import llm_service

logger = logging.getLogger(__name__)

# Track last summary generation time
_last_summary_time = None


class ReminderSchedulerService:
    """
    Service for scheduling and dispatching:
    1. Email/WhatsApp reminders (every 1-5 minutes)
    2. Message summarization (every 6 hours)
    """

    def __init__(self, db: Session):
        self.db = db
        self._whatsapp_service = whatsapp_service
        self._email_service = email_service

    async def process_scheduled_tasks(self):
        """
        ✅ NEW: Main scheduler job combining reminders + summaries
        Called frequently (every 1-5 minutes by APScheduler)
        """
        try:
            # Always process reminders
            await self.send_reminders()
            
            # Process summaries only every 6 hours
            await self.process_message_summaries_if_due()
        
        except Exception as e:
            logger.exception(
                "Fatal error in scheduler",
                extra={"error": str(e)}
            )

    # ─────────────────────────────────────────────────────────
    # REMINDER LOGIC (existing)
    # ─────────────────────────────────────────────────────────

    async def send_reminders(self):
        """
        Find and send due reminders via email/WhatsApp.
        Runs every 1-5 minutes.
        
        CRITICAL: Marks reminders as sent after dispatch
        to prevent duplicate sends.
        """
        try:
            logger.info("📌 Checking for due reminders...")
            
            now = datetime.utcnow()
            
            # Find UNSENT reminders past their scheduled time
            unsent_reminders = self.db.query(Reminder).filter(
                Reminder.scheduled_at <= now,
                Reminder.sent_at == None
            ).all()
            
            if not unsent_reminders:
                logger.debug("No reminders to send")
                return
            
            logger.info(f"📌 Found {len(unsent_reminders)} reminders to send")
            
            for reminder in unsent_reminders:
                try:
                    user = self.db.query(User).filter(
                        User.id == reminder.user_id
                    ).first()
                    
                    if not user:
                        logger.warning(f"User {reminder.user_id} not found")
                        reminder.sent_at = datetime.utcnow()
                        self.db.commit()
                        continue
                    
                    channels_sent = []
                    send_success = False
                    
                    # Try email
                    if user.email:
                        try:
                            email_result = await self._dispatch_email(
                                user.email, 
                                reminder.content
                            )
                            if email_result:
                                channels_sent.append("email")
                                send_success = True
                        except Exception as e:
                            logger.error(
                                f"Email dispatch failed for reminder {reminder.id}: {e}",
                                extra={
                                    "reminder_id": reminder.id,
                                    "user_id": reminder.user_id,
                                }
                            )
                    
                    # Try WhatsApp
                    if user.phone_number:
                        try:
                            whatsapp_result = await self._dispatch_whatsapp(
                                user.phone_number, 
                                reminder.content
                            )
                            if whatsapp_result:
                                channels_sent.append("whatsapp")
                                send_success = True
                        except Exception as e:
                            logger.error(
                                f"WhatsApp dispatch failed for reminder {reminder.id}: {e}",
                                extra={
                                    "reminder_id": reminder.id,
                                    "user_id": reminder.user_id,
                                }
                            )
                    
                    # Mark as sent to prevent resending
                    reminder.sent_at = datetime.utcnow()
                    self.db.commit()
                    
                    logger.info(
                        f"✅ Reminder {reminder.id} sent via {'+'.join(channels_sent) or 'none'}",
                        extra={
                            "reminder_id": reminder.id,
                            "user_id": reminder.user_id,
                            "channels": "+".join(channels_sent) if channels_sent else "none",
                        }
                    )
                
                except Exception as e:
                    logger.exception(
                        f"Error processing reminder {reminder.id}: {e}",
                        extra={"reminder_id": reminder.id}
                    )
                    try:
                        reminder.sent_at = datetime.utcnow()
                        self.db.commit()
                    except Exception as ce:
                        logger.error(f"Failed to mark reminder as sent: {ce}")
        
        except Exception as e:
            logger.exception(f"Fatal error in send_reminders: {e}")

    async def _dispatch_email(self, email: str, content: str) -> bool:
        """Dispatch reminder via email"""
        try:
            result = self._email_service.send_reminder(
                user_email=email,
                content=content
            )
            logger.info(f"Email sent to {email[:3]}***")
            return result
        except Exception as e:
            logger.error(f"Email dispatch error: {e}")
            return False

    async def _dispatch_whatsapp(self, phone: str, content: str) -> bool:
        """Dispatch reminder via WhatsApp"""
        try:
            await self._whatsapp_service.send_message(phone, content)
            logger.info(f"WhatsApp sent to {phone[:4]}****{phone[-2:]}")
            return True
        except Exception as e:
            logger.error(f"WhatsApp dispatch error: {e}")
            return False

    # ─────────────────────────────────────────────────────────
    # MESSAGE SUMMARIZATION LOGIC (NEW)
    # ─────────────────────────────────────────────────────────

    async def process_message_summaries_if_due(self):
        """
        ✅ NEW: Generate message summaries every 6 hours
        (Even though called every 1-5 minutes, only runs every 6 hours)
        
        This runs inside the same scheduler as reminders!
        """
        global _last_summary_time
        
        now = datetime.utcnow()
        
        # Check if 6 hours have passed since last summary generation
        if _last_summary_time is None:
            # First time - run now
            logger.info("📝 First summary run - generating...")
        elif now - _last_summary_time < timedelta(hours=6):
            # Not yet time - skip
            time_until_next = _last_summary_time + timedelta(hours=6) - now
            logger.debug(
                f"⏳ Summary generation not due yet "
                f"({time_until_next.total_seconds() / 3600:.1f} hours remaining)"
            )
            return
        
        # Time to generate summaries!
        logger.info("📝 Starting message summarization task...")
        _last_summary_time = now
        
        await self._generate_message_summaries()

    async def _generate_message_summaries(self):
        """
        ✅ NEW: Generate summaries for ALL users
        ✅ FIXED: Removed is_active filter (User model doesn't have it)
        """
        try:
            # ✅ FIX: Get ALL users (no is_active filter)
            users = self.db.query(User).all()
            logger.info(f"📋 Processing {len(users)} users for summarization")
            
            success_count = 0
            error_count = 0
            
            for user in users:
                try:
                    await self._summarize_user(user)
                    success_count += 1
                except Exception as e:
                    logger.error(
                        f"❌ Failed to summarize user {user.id}: {e}",
                        extra={"user_id": user.id},
                        exc_info=True
                    )
                    error_count += 1
            
            logger.info(
                f"✅ Summarization complete: {success_count} succeeded, {error_count} failed",
                extra={"success": success_count, "errors": error_count}
            )
        
        except Exception as e:
            logger.exception(f"❌ Fatal error in summarization: {e}")

    async def _summarize_user(self, user):
        """Generate/update summary for one user"""
        user_id = user.id
        
        try:
            # Step 1: Get last 500 messages
            messages = MessageRepository.get_last_500(self.db, user_id)
            
            if len(messages) < 50:
                logger.debug(f"⏭️  User {user_id}: only {len(messages)} messages, skipping")
                return
            
            # Step 2: Convert to dict format
            msg_dicts = [
                {
                    "role": "assistant" if m.role == "assistant" else "user",
                    "content": m.content or ""
                }
                for m in messages
            ]
            
            # Step 3: Get previous summary
            previous_summary = await memory_service.get_latest_by_type(user_id, "summary")
            
            # Step 4: Generate new summary (integrating old + new)
            logger.debug(
                f"📝 Generating summary for user {user_id}",
                extra={"messages": len(msg_dicts), "has_previous": bool(previous_summary)}
            )
            
            summary = await llm_service.summarize_messages(
                msg_dicts,
                previous_summary=previous_summary
            )
            
            # Step 5: Embed and store
            vector = await embedding_service.embed(summary)
            if not vector:
                logger.error(f"❌ User {user_id}: Failed to embed summary")
                return
            
            stored = await memory_service.store_summary(user_id, summary, vector)
            
            if stored:
                if previous_summary:
                    logger.info(
                        f"✅ User {user_id}: Summary updated",
                        extra={"user_id": user_id, "length": len(summary)}
                    )
                else:
                    logger.info(
                        f"✅ User {user_id}: Initial summary created",
                        extra={"user_id": user_id, "length": len(summary)}
                    )
            else:
                logger.error(f"❌ User {user_id}: Failed to store summary")
                return
            
            # Step 6: Premium users - extract preferences
            if hasattr(user, 'profile') and hasattr(user.profile, 'account_tier'):
                if (user.profile.account_tier or "").strip() == "premium":
                    await self._extract_and_store_preferences(user_id, msg_dicts)
        
        except Exception as e:
            logger.error(
                f"❌ Error summarizing user {user_id}: {e}",
                extra={"user_id": user_id},
                exc_info=True
            )

    async def _extract_and_store_preferences(self, user_id: int, messages):
        """Extract preferences from conversation and store in Qdrant"""
        try:
            logger.debug(f"🔍 Extracting preferences for user {user_id}")
            
            prefs = await llm_service.extract_preferences(messages)
            
            if not prefs:
                logger.debug(f"⚠️  User {user_id}: No preferences extracted")
                return
            
            stored_count = 0
            for pref in prefs:
                pref_vector = await embedding_service.embed(pref)
                if pref_vector:
                    stored = await memory_service.store_preference(user_id, pref, pref_vector)
                    if stored:
                        stored_count += 1
            
            if stored_count > 0:
                logger.info(
                    f"✅ User {user_id}: {stored_count} preferences stored",
                    extra={"user_id": user_id, "count": stored_count}
                )
        
        except Exception as e:
            logger.error(
                f"❌ Preference extraction failed for user {user_id}: {e}",
                extra={"user_id": user_id},
                exc_info=True
            )


# Singleton instance
_reminder_scheduler_service = None


def get_reminder_scheduler_service(db: Session) -> ReminderSchedulerService:
    """Get or create reminder scheduler service"""
    global _reminder_scheduler_service
    if _reminder_scheduler_service is None:
        _reminder_scheduler_service = ReminderSchedulerService(db)
    return _reminder_scheduler_service