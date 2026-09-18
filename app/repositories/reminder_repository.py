"""
Reminder Repository
P0-5 FIX: Atomic job claim with SELECT FOR UPDATE SKIP LOCKED, where the
claimed rows are flipped to PROCESSING *inside the same transaction* as
the claim - so by the time the row locks release at commit, the rows no
longer match the claim query's WHERE clause and can't be re-claimed.
P0-4 FIX: finalize_attempt() replaces the old "always mark sent_at"
behavior with a real per-channel state machine (SENT / PARTIALLY_SENT /
PENDING / FAILED), bounded retries, and a fixed retry delay.
P0-5 (crash recovery) FIX: recover_stale_processing() unsticks reminders
left at PROCESSING by a worker that died mid-attempt.

File location: app/repositories/reminder_repository.py
"""
import logging
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import desc
from sqlalchemy.orm import Session
from app.models.database import Reminder

logger = logging.getLogger(__name__)


class ReminderRepository:
    """Repository for reminder operations with atomic claims and honest retry state."""

    # Bounded retries: after this many attempts a still-incomplete
    # reminder is marked permanently FAILED instead of retrying forever.
    MAX_ATTEMPTS = 5

    # Fixed delay before a PENDING/PARTIALLY_SENT reminder becomes
    # claimable again after a failed attempt.
    #
    # Honesty note: this is a FIXED delay, not exponential backoff. True
    # exponential backoff (delay = base * 2**attempt_count) is easy to
    # compute in Python but awkward to express as a single SQL WHERE
    # clause per-row without a generated/computed column or a raw SQL
    # expression; a fixed delay was chosen to keep this correct and
    # readable rather than clever. If you need real backoff, compute the
    # cutoff per-row after the initial fetch and filter in Python, or add
    # a generated column.
    RETRY_DELAY = timedelta(minutes=5)

    # How long a reminder can sit at PROCESSING before we assume the
    # worker that claimed it crashed and it's safe to reclaim.
    STALE_PROCESSING = timedelta(minutes=10)

    # ------------------------------------------------------------------ #
    # Creation / plain reads (unchanged from before)
    # ------------------------------------------------------------------ #

    @staticmethod
    def create(db: Session, user_id: int, content: str, scheduled_at, timezone: str = "UTC"):
        """Create a new reminder"""
        reminder = Reminder(
            user_id=user_id,
            content=content,
            scheduled_at=scheduled_at,
            timezone=timezone,
            status="PENDING",
            email_status="PENDING",
            whatsapp_status="PENDING",
            attempt_count=0,
        )
        db.add(reminder)
        db.commit()
        db.refresh(reminder)
        logger.info(f"✅ Reminder created: {reminder.id}")
        return reminder

    @staticmethod
    def get_by_user(db: Session, user_id: int):
        """Get all reminders for a user"""
        return db.query(Reminder).filter(
            Reminder.user_id == user_id
        ).order_by(desc(Reminder.created_at)).all()

    @staticmethod
    def get_by_id(db: Session, reminder_id: int):
        """Get reminder by ID"""
        return db.query(Reminder).filter(Reminder.id == reminder_id).first()

    @staticmethod
    def get_pending(db: Session, limit: int = 10):
        """
        Non-atomic convenience read (dashboards/debugging only).
        Use claim_next_pending() for anything that will actually process
        a reminder - this one does not lock or claim rows.
        """
        return db.query(Reminder).filter(
            Reminder.status == "PENDING",
            Reminder.scheduled_at <= datetime.utcnow(),
        ).order_by(Reminder.scheduled_at).limit(limit).all()

    # ------------------------------------------------------------------ #
    # P0-5: atomic claim
    # ------------------------------------------------------------------ #

    @staticmethod
    def claim_next_pending(db: Session, limit: int = 10):
        """
        Atomically claim up to `limit` due reminders.

        Two things make this actually exactly-once, not just "uses
        FOR UPDATE":
          1. SELECT ... FOR UPDATE SKIP LOCKED so two workers running this
             concurrently never lock the same row.
          2. The claimed rows are flipped to status='PROCESSING' via an
             UPDATE *inside this same transaction*, committed together
             with the claim. Postgres releases row locks at COMMIT - if
             we only held the lock and never changed status, a second
             worker's next poll (a fresh SELECT ... WHERE status='PENDING')
             would see the same still-PENDING rows again the instant this
             transaction commits. Flipping status first means that by the
             time the lock is gone, the row no longer matches anyone
             else's claim query.

        Only claims reminders under MAX_ATTEMPTS, and only reclaims a
        previously-failed one after RETRY_DELAY has passed since its last
        attempt.
        """
        now = datetime.utcnow()
        retry_cutoff = now - ReminderRepository.RETRY_DELAY

        try:
            candidates = db.query(Reminder).filter(
                Reminder.status.in_(["PENDING", "PARTIALLY_SENT"]),
                Reminder.scheduled_at <= now,
                Reminder.attempt_count < ReminderRepository.MAX_ATTEMPTS,
                (Reminder.last_attempt_at.is_(None)) | (Reminder.last_attempt_at <= retry_cutoff),
            ).with_for_update(skip_locked=True).order_by(
                Reminder.scheduled_at
            ).limit(limit).all()

            if not candidates:
                logger.debug("No pending reminders to claim")
                return []

            ids = [r.id for r in candidates]

            db.query(Reminder).filter(Reminder.id.in_(ids)).update(
                {
                    Reminder.status: "PROCESSING",
                    Reminder.attempt_count: Reminder.attempt_count + 1,
                    Reminder.last_attempt_at: now,
                },
                synchronize_session=False,
            )
            db.commit()

            # Re-fetch so the caller sees the post-update attempt_count /
            # status rather than the pre-update snapshot from `candidates`.
            claimed = db.query(Reminder).filter(Reminder.id.in_(ids)).all()

            logger.info(f"📌 Claimed {len(claimed)} reminder(s) for processing")
            return claimed

        except Exception as e:
            db.rollback()
            logger.error(f"❌ Failed to claim reminders: {e}", exc_info=True)
            return []

    # ------------------------------------------------------------------ #
    # P0-4: honest per-channel completion state
    # ------------------------------------------------------------------ #

    @staticmethod
    def finalize_attempt(
        db: Session,
        reminder_id: int,
        email_result: Optional[str] = None,
        whatsapp_result: Optional[str] = None,
        failure_reason: Optional[str] = None,
    ):
        """
        Record the outcome of one processing attempt and decide the
        reminder's next status. Never sets sent_at unless delivery is
        actually complete.

        email_result / whatsapp_result: one of
          "sent"   - attempted this round and succeeded
          "failed" - attempted this round and failed
          "skip"   - not applicable for this user (no email / no phone) -
                     counts as satisfied without ever being attempted
          None     - not attempted this round (e.g. already SENT on a
                     previous pass, so it was left alone) - keeps
                     whatever status that channel already had

        Resulting reminder.status:
          SENT            - every applicable channel is SENT or SKIPPED
          FAILED          - attempt_count has hit MAX_ATTEMPTS and it's
                             still not fully sent (permanent, not retried)
          PARTIALLY_SENT  - at least one channel SENT, at least one still
                             outstanding, retries remain
          PENDING         - no channel has succeeded yet, retries remain
        """
        reminder = db.query(Reminder).filter(Reminder.id == reminder_id).first()
        if not reminder:
            logger.error(f"finalize_attempt: reminder {reminder_id} not found")
            return None

        status_map = {"sent": "SENT", "failed": "FAILED", "skip": "SKIPPED"}

        if email_result in status_map:
            reminder.email_status = status_map[email_result]
        if whatsapp_result in status_map:
            reminder.whatsapp_status = status_map[whatsapp_result]

        channel_statuses = [reminder.email_status, reminder.whatsapp_status]
        all_done = all(s in ("SENT", "SKIPPED") for s in channel_statuses)
        any_sent = any(s == "SENT" for s in channel_statuses)

        if all_done:
            reminder.status = "SENT"
            reminder.sent_at = datetime.utcnow()
            reminder.last_failure_reason = None
        elif reminder.attempt_count >= ReminderRepository.MAX_ATTEMPTS:
            reminder.status = "FAILED"
            reminder.last_failure_reason = failure_reason or "Max retry attempts exceeded"
        elif any_sent:
            reminder.status = "PARTIALLY_SENT"
            reminder.last_failure_reason = failure_reason
        else:
            reminder.status = "PENDING"
            reminder.last_failure_reason = failure_reason

        db.flush()
        logger.info(
            f"Reminder {reminder_id} -> {reminder.status} "
            f"(email={reminder.email_status}, whatsapp={reminder.whatsapp_status}, "
            f"attempt={reminder.attempt_count}/{ReminderRepository.MAX_ATTEMPTS})"
        )
        return reminder

    # ------------------------------------------------------------------ #
    # P0-5 (crash recovery): stale PROCESSING rows
    # ------------------------------------------------------------------ #

    @staticmethod
    def recover_stale_processing(db: Session):
        """
        A reminder gets stuck at PROCESSING forever if the worker that
        claimed it (claim_next_pending) crashes before finalize_attempt()
        runs. Anything that's been PROCESSING longer than
        STALE_PROCESSING is treated as a failed attempt: it's either sent
        back to PENDING/PARTIALLY_SENT (so it becomes claimable again
        after the normal retry delay) or, if it's already exhausted its
        retries, marked permanently FAILED. Channel statuses that already
        succeeded before the crash are preserved.
        """
        stale_cutoff = datetime.utcnow() - ReminderRepository.STALE_PROCESSING
        try:
            stale = db.query(Reminder).filter(
                Reminder.status == "PROCESSING",
                Reminder.last_attempt_at <= stale_cutoff,
            ).with_for_update(skip_locked=True).all()

            if not stale:
                return

            for reminder in stale:
                if reminder.attempt_count >= ReminderRepository.MAX_ATTEMPTS:
                    reminder.status = "FAILED"
                    reminder.last_failure_reason = "Worker crashed / stale claim, retries exhausted"
                else:
                    any_sent = reminder.email_status == "SENT" or reminder.whatsapp_status == "SENT"
                    reminder.status = "PARTIALLY_SENT" if any_sent else "PENDING"
                    reminder.last_failure_reason = "Worker crashed / stale claim, recovered"

            db.commit()
            logger.warning(f"♻️  Recovered {len(stale)} stale PROCESSING reminder(s)")

        except Exception as e:
            db.rollback()
            logger.error(f"Failed to recover stale reminders: {e}", exc_info=True)

    # ------------------------------------------------------------------ #
    # Legacy methods - kept for any other caller that still uses them.
    # Prefer claim_next_pending() + finalize_attempt() for new code; they
    # don't participate in the atomic claim / retry-state-machine above.
    # ------------------------------------------------------------------ #

    @staticmethod
    def mark_processing(db: Session, reminder_id: int):
        """Legacy: prefer claim_next_pending(), which already does this atomically."""
        try:
            reminder = db.query(Reminder).filter(Reminder.id == reminder_id).first()
            if reminder:
                reminder.status = "PROCESSING"
                reminder.attempt_count = (reminder.attempt_count or 0) + 1
                reminder.last_attempt_at = datetime.utcnow()
                db.flush()
                return reminder
        except Exception as e:
            logger.error(f"Failed to mark reminder as processing: {e}")
        return None

    @staticmethod
    def mark_sent(db: Session, reminder_id: int, email_sent: bool = False, whatsapp_sent: bool = False):
        """Legacy: prefer finalize_attempt(), which supports partial success and retries."""
        try:
            reminder = db.query(Reminder).filter(Reminder.id == reminder_id).first()
            if reminder:
                reminder.status = "SENT"
                reminder.sent_at = datetime.utcnow()
                reminder.email_status = "SENT" if email_sent else "FAILED"
                reminder.whatsapp_status = "SENT" if whatsapp_sent else "FAILED"
                db.flush()
                logger.info(f"✅ Reminder {reminder_id} marked SENT")
                return reminder
        except Exception as e:
            logger.error(f"Failed to mark reminder as sent: {e}")
        return None

    @staticmethod
    def mark_failed(db: Session, reminder_id: int, reason: str = None):
        """Legacy: prefer finalize_attempt()."""
        try:
            reminder = db.query(Reminder).filter(Reminder.id == reminder_id).first()
            if reminder:
                reminder.status = "FAILED"
                reminder.last_failure_reason = reason
                db.flush()
                logger.warning(f"⚠️  Reminder {reminder_id} marked FAILED: {reason}")
                return reminder
        except Exception as e:
            logger.error(f"Failed to mark reminder as failed: {e}")
        return None

    @staticmethod
    def update_status(db: Session, reminder_id: int, status: str, **kwargs):
        """Update reminder status and optional fields"""
        try:
            reminder = db.query(Reminder).filter(Reminder.id == reminder_id).first()
            if reminder:
                reminder.status = status
                for key, value in kwargs.items():
                    if hasattr(reminder, key):
                        setattr(reminder, key, value)
                db.flush()
                return reminder
        except Exception as e:
            logger.error(f"Failed to update reminder status: {e}")
        return None