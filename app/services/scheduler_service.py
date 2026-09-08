
import asyncio
import inspect
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from apscheduler.events import EVENT_JOB_ERROR
from apscheduler.schedulers.background import BackgroundScheduler

from app.db.database import SessionLocal
from app.models.database import Reminder, User
from app.services.email_service import email_service

logger = logging.getLogger(__name__)

BATCH_LIMIT = 50
LOOKBACK_HOURS = 24
MAX_ATTEMPTS = 3
FAILURE_MAP_CAP = 5000          # stop the dict growing without bound


def _utcnow_naive() -> datetime:
    """Naive UTC — matches your DateTime columns (no timezone=True)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SchedulerService:
    def __init__(self):
        self.scheduler = BackgroundScheduler(daemon=True, timezone="UTC")
        self._failures: dict[int, int] = defaultdict(int)

    # ------------------------------------------------------------------ start
    def start(self):
        if self.scheduler.running:
            logger.warning("Scheduler already running; ignoring start()")
            return

        self.scheduler.add_job(
            self.send_due_reminders,
            trigger="interval",
            minutes=1,
            id="send_reminders",
            replace_existing=True,
            max_instances=1,                 # never overlap runs
            coalesce=True,                   # collapse missed runs into one
            misfire_grace_time=300,
            next_run_time=_utcnow_naive(),   # fire once immediately on boot
        )
        self.scheduler.add_listener(self._on_job_error, EVENT_JOB_ERROR)
        self.scheduler.start()

        # APScheduler swallows job exceptions into its own logger. Without
        # this, a crashing job is indistinguishable from an idle one.
        logging.getLogger("apscheduler").setLevel(logging.INFO)
        logger.info(
            "Scheduler started with %d job(s): %s",
            len(self.scheduler.get_jobs()),
            [j.id for j in self.scheduler.get_jobs()],
        )

    def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    @staticmethod
    def _on_job_error(event):
        logger.error("Scheduler job %s crashed", event.job_id, exc_info=event.exception)

    # ------------------------------------------------------------------- send
    def send_due_reminders(self):
        now = _utcnow_naive()
        window_start = now - timedelta(hours=LOOKBACK_HOURS)
        db = SessionLocal()
        try:
            due = (
                db.query(Reminder)
                .filter(Reminder.sent_at.is_(None))
                .filter(Reminder.scheduled_at <= now)
                .filter(Reminder.scheduled_at >= window_start)
                .order_by(Reminder.scheduled_at)
                .limit(BATCH_LIMIT)
                .all()
            )
            if not due:
                logger.debug("No reminders due at %s UTC", now)
                return

            logger.info("Found %d reminder(s) due", len(due))

            for reminder in due:
                if self._failures[reminder.id] >= MAX_ATTEMPTS:
                    logger.warning(
                        "Reminder %s exceeded %d attempts; skipping",
                        reminder.id, MAX_ATTEMPTS,
                    )
                    continue
                try:
                    user = db.query(User).filter(User.id == reminder.user_id).first()
                    if not user or not user.email:
                        logger.warning(
                            "Reminder %s has no deliverable user; will not retry", reminder.id
                        )
                        self._failures[reminder.id] = MAX_ATTEMPTS
                        continue

                    self._dispatch(user.email, reminder.content)

                    # Only mark sent AFTER a confirmed send.
                    reminder.sent_at = _utcnow_naive()
                    db.commit()
                    self._failures.pop(reminder.id, None)
                    logger.info("Reminder %s sent to %s", reminder.id, user.email)

                except Exception:
                    db.rollback()
                    self._failures[reminder.id] += 1
                    logger.exception(
                        "Failed to send reminder %s (attempt %d/%d)",
                        reminder.id, self._failures[reminder.id], MAX_ATTEMPTS,
                    )
        finally:
            db.close()
            self._trim_failures()

    def _trim_failures(self):
        if len(self._failures) > FAILURE_MAP_CAP:
            logger.warning("Failure map exceeded %d entries; clearing", FAILURE_MAP_CAP)
            self._failures.clear()

    @staticmethod
    def _dispatch(email: str, content: str):
        """
        email_service.send_reminder may be sync or async. Awaiting an async
        function without asyncio would silently return an un-awaited coroutine
        and send nothing while marking the row as sent.

        A falsy result means the provider rejected the send — raise so the
        caller counts it as a failure instead of marking it delivered.
        """
        result = email_service.send_reminder(email, content)
        if inspect.isawaitable(result):
            # Worker thread has no running event loop, so this is safe.
            result = asyncio.run(result)
        if result is False:
            raise RuntimeError(f"email_service refused the send to {email}")


scheduler_service = SchedulerService()