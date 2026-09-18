import asyncio
import inspect
import logging
import signal
import os
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SchedulerWorker:
    """Dedicated scheduler worker - runs in separate process"""

    def __init__(self):
        self.running = False
        self.db_session = None
        self.SessionLocal = None

    def initialize_database(self):
        """Initialize database connection"""
        from app.core.config import settings

        logger.info("Initializing database connection...")
        engine = create_engine(
            settings.DATABASE_URL,
            echo=False,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        logger.info("✅ Database initialized")

    async def start(self):
        """Start the scheduler worker"""
        logger.info("=" * 70)
        logger.info("🚀 SCHEDULER WORKER: Starting")
        logger.info("=" * 70)

        self.initialize_database()
        self.running = True

        # Run jobs in background
        await asyncio.gather(
            self._run_reminder_loop(),
            self._run_summary_loop(),
        )

    async def _run_reminder_loop(self):
        """
        Process reminders every 1 minute.
        P0-5: atomic claims via SELECT FOR UPDATE SKIP LOCKED, with the
        claimed rows flipped to PROCESSING inside the same transaction as
        the claim (see reminder_repository.claim_next_pending).
        P0-5 (recovery): before claiming anything new, unstick any
        reminder a crashed worker left behind at PROCESSING.
        """
        from app.repositories.reminder_repository import ReminderRepository

        logger.info("📌 Reminder processing loop started (every 60 seconds)")

        while self.running:
            db = self.SessionLocal()
            try:
                ReminderRepository.recover_stale_processing(db)

                claimed_reminders = ReminderRepository.claim_next_pending(db, limit=10)

                if not claimed_reminders:
                    logger.debug("No reminders to process")
                    await asyncio.sleep(60)
                    continue

                logger.info(f"📌 Processing {len(claimed_reminders)} reminders")

                for reminder in claimed_reminders:
                    try:
                        await self._process_reminder(db, reminder)
                    except Exception as e:
                        logger.error(
                            f"❌ Error processing reminder {reminder.id}: {e}",
                            extra={"reminder_id": reminder.id},
                            exc_info=True
                        )
                        ReminderRepository.finalize_attempt(
                            db, reminder.id,
                            email_result="failed",
                            whatsapp_result="failed",
                            failure_reason=f"Processing error: {str(e)[:150]}",
                        )
                    finally:
                        db.commit()

                await asyncio.sleep(60)

            except Exception as e:
                logger.error(f"Fatal error in reminder loop: {e}", exc_info=True)
                await asyncio.sleep(60)

            finally:
                db.close()

    async def _process_reminder(self, db, reminder):
        """
        Process a single reminder.
        P0-4: records a real per-channel outcome via finalize_attempt()
        instead of unconditionally stamping sent_at. A channel already at
        SENT from a previous partial attempt is skipped, not re-sent.

        BUG FIX (found from a real production log): email_service and
        whatsapp_service are both called via `await service.method(...)`,
        which assumes the method is `async def`. That was true of the
        email_service stub I originally saw, but this file doesn't
        control app/services/email_service.py - if the real
        implementation you're running is a plain sync `def` (e.g. a
        Brevo integration written later, not async), `await <bool>`
        raises TypeError immediately AFTER the real send already
        happened. That's worse than a no-op: the send succeeds, gets
        recorded as "failed" anyway, and the next retry sends it AGAIN -
        duplicate emails, every retry, for a channel that was never
        actually broken. Both calls now go through _maybe_await(), which
        only awaits the result if it's actually awaitable, so this works
        correctly whether the underlying service method is sync or
        async - without needing to know which.
        """
        from app.repositories.reminder_repository import ReminderRepository
        from app.models.database import User

        logger.info(f"Processing reminder {reminder.id} (attempt {reminder.attempt_count})...")

        user = db.query(User).filter(User.id == reminder.user_id).first()
        if not user:
            logger.error(f"User {reminder.user_id} not found")
            ReminderRepository.finalize_attempt(
                db, reminder.id,
                email_result="failed",
                whatsapp_result="failed",
                failure_reason="User not found",
            )
            return

        email_result = None
        whatsapp_result = None
        failure_reason = None

        # --- Email ---
        if reminder.email_status == "SENT":
            pass  # already delivered on a previous attempt
        elif not user.email:
            email_result = "skip"
        else:
            try:
                from app.services.email_service import email_service
                success = await self._maybe_await(email_service.send_reminder(
                    user_email=user.email,
                    content=reminder.content,
                ))
                email_result = "sent" if success else "failed"
                if success:
                    logger.info(f"✅ Email sent for reminder {reminder.id}")
                else:
                    failure_reason = "Email delivery failed"
                    logger.warning(f"Email failed for reminder {reminder.id}")
            except Exception as e:
                email_result = "failed"
                failure_reason = f"Email error: {str(e)[:150]}"
                logger.error(f"❌ Email error: {e}")

        # --- WhatsApp ---
        if reminder.whatsapp_status == "SENT":
            pass  # already delivered on a previous attempt
        elif not user.phone_number:
            whatsapp_result = "skip"
        else:
            try:
                from app.services.whatsapp_service import whatsapp_service

                # Free-form text, NOT a template - by explicit choice,
                # to avoid depending on Meta template approval.
                #
                # Real limitation this creates, not a bug: WhatsApp
                # Business Platform only allows free-form sends to a
                # user within 24 hours of THEIR last message to your
                # number. A reminder that fires more than 24h after this
                # user last messaged the bot on WhatsApp - which is most
                # reminders, since they're commonly set a day or more
                # ahead - will be rejected by Meta (typically error
                # 131047, "re-engagement message"), not silently
                # succeed. finalize_attempt() below still records that
                # correctly as a failure and retries it, but it will
                # keep failing for the same platform reason every time,
                # up to attempt_count hitting MAX_ATTEMPTS and going to
                # permanent FAILED. There is no code-level workaround
                # for this specific case - the original P0-10 template
                # approach existed precisely to get around it, and
                # that's the only thing on WhatsApp's side that can.
                message_text = f"⏰ Reminder: {reminder.content}"
                success = await self._maybe_await(whatsapp_service.send_message(
                    user.phone_number, message_text,
                ))
                whatsapp_result = "sent" if success else "failed"
                if success:
                    logger.info(f"✅ WhatsApp sent for reminder {reminder.id}")
                else:
                    failure_reason = failure_reason or "WhatsApp delivery failed"
                    logger.warning(f"WhatsApp failed for reminder {reminder.id}")
            except Exception as e:
                whatsapp_result = "failed"
                failure_reason = failure_reason or f"WhatsApp error: {str(e)[:150]}"
                logger.error(f"❌ WhatsApp error: {e}")

        ReminderRepository.finalize_attempt(
            db, reminder.id,
            email_result=email_result,
            whatsapp_result=whatsapp_result,
            failure_reason=failure_reason,
        )

    @staticmethod
    async def _maybe_await(value):
        """
        Call site can't be sure whether a given service method is
        `async def` (returns a coroutine) or a plain `def` (returns the
        value directly) - await it only if it's actually awaitable.
        Calling the method itself (email_service.send_reminder(...))
        happens at the call site BEFORE this runs either way, so the
        real work (the actual send) has already happened by the time
        this decides whether there's anything left to await.
        """
        if inspect.isawaitable(value):
            return await value
        return value

    async def _run_summary_loop(self):
        """
        Process message summaries every 6 hours.
        P0-12: cursor-based incremental summarization.
        """
        logger.info("📝 Summary processing loop started (every 6 hours)")

        while self.running:
            db = self.SessionLocal()
            try:
                logger.info("Starting message summarization batch...")

                from app.models.database import User
                users = db.query(User).all()

                logger.info(f"Processing {len(users)} users for summarization")

                success_count = 0
                for user in users:
                    try:
                        await self._summarize_user(db, user)
                        success_count += 1
                    except Exception as e:
                        logger.error(
                            f"Failed to summarize user {user.id}: {e}",
                            extra={"user_id": user.id},
                            exc_info=True,
                        )

                logger.info(f"✅ Summarization complete: {success_count}/{len(users)} succeeded")

                await asyncio.sleep(6 * 60 * 60)

            except Exception as e:
                logger.error(f"Fatal error in summary loop: {e}", exc_info=True)
                await asyncio.sleep(60 * 60)

            finally:
                db.close()

    async def _summarize_user(self, db, user):
        """
        Generate or update the summary for one user.
        P0-12: only looks at messages since last_summarized_message_id
        (cursor), chunked to a char budget, actually calls the LLM, and
        restores premium preference extraction.
        P1-20: both the summarization and preference-extraction LLM
        calls now pass usage_context so background work is recorded in
        llm_usage too, not just interactive chat turns.
        """
        from app.models.database import Message, MemorySummary, UserProfile
        from app.services.llm_service import llm_service

        CHUNK_CHAR_BUDGET = 12000  # conservative ~3000 tokens per chunk
        MAX_MESSAGE_CHARS = 2000   # guard against one pathologically long message

        try:
            last_summary = db.query(MemorySummary).filter(
                MemorySummary.user_id == user.id
            ).order_by(MemorySummary.created_at.desc()).first()

            last_message_id = last_summary.last_summarized_message_id if last_summary else 0

            new_messages = db.query(Message).filter(
                Message.user_id == user.id,
                Message.id > last_message_id,
            ).order_by(Message.created_at).limit(500).all()

            if len(new_messages) < 10:
                logger.debug(f"User {user.id}: only {len(new_messages)} new messages, skipping")
                return

            logger.info(f"User {user.id}: {len(new_messages)} new messages to summarize")

            chunks = []
            current, current_len = [], 0
            for m in new_messages:
                content = (m.content or "")[:MAX_MESSAGE_CHARS]
                entry = {"role": "assistant" if m.role == "assistant" else "user", "content": content}
                entry_len = len(content) + 20
                if current and current_len + entry_len > CHUNK_CHAR_BUDGET:
                    chunks.append(current)
                    current, current_len = [], 0
                current.append(entry)
                current_len += entry_len
            if current:
                chunks.append(current)

            usage_context = {"db": db, "user_id": user.id, "purpose": "summarization"}

            running_summary = last_summary.summary_text if last_summary else None
            for chunk in chunks:
                running_summary = await llm_service.summarize_messages(
                    chunk, previous_summary=running_summary, usage_context=usage_context
                )

            summary = MemorySummary(
                user_id=user.id,
                summary_text=running_summary,
                last_summarized_message_id=new_messages[-1].id,
                token_count=len(running_summary or "") // 4,
            )
            db.add(summary)
            db.commit()

            logger.info(f"✅ User {user.id}: summary updated ({len(chunks)} chunk(s))")

            # Premium users: extract preferences from the same new-message window.
            profile = db.query(UserProfile).filter(UserProfile.user_id == user.id).first()
            if profile and (profile.account_tier or "").strip() == "premium":
                await self._extract_and_store_preferences(db, user.id, [
                    {"role": "assistant" if m.role == "assistant" else "user",
                     "content": (m.content or "")[:MAX_MESSAGE_CHARS]}
                    for m in new_messages
                ])

        except Exception as e:
            logger.error(f"Error summarizing user {user.id}: {e}", exc_info=True)
            db.rollback()

    async def _extract_and_store_preferences(self, db, user_id: int, messages):
        """
        Extract preferences from the conversation and store them in
        Qdrant (premium only). Relies on llm_service.extract_preferences(),
        fixed to actually accept array responses (see llm_service.py).
        """
        from app.services.llm_service import llm_service
        from app.services.embedding_service import embedding_service
        from app.services.memory_service import memory_service

        try:
            prefs = await llm_service.extract_preferences(
                messages,
                usage_context={"db": db, "user_id": user_id, "purpose": "preference_extraction"},
            )
            if not prefs:
                logger.debug(f"User {user_id}: no preferences extracted")
                return

            stored = 0
            for pref in prefs:
                vector = await embedding_service.embed(pref)
                if vector and await memory_service.store_preference(user_id, pref, vector):
                    stored += 1

            if stored:
                logger.info(f"✅ User {user_id}: {stored} preference(s) stored")

        except Exception as e:
            logger.error(f"Preference extraction failed for user {user_id}: {e}", exc_info=True)

    async def stop(self):
        """Stop the scheduler"""
        logger.info("⏹️  Shutting down scheduler...")
        self.running = False
        await asyncio.sleep(1)
        logger.info("👋 Scheduler stopped")


async def main():
    """Main entry point"""
    worker = SchedulerWorker()

    def signal_handler(sig, frame):
        logger.info("SIGTERM/SIGINT received - shutting down...")
        asyncio.create_task(worker.stop())

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        await worker.start()
    except KeyboardInterrupt:
        await worker.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutdown complete")