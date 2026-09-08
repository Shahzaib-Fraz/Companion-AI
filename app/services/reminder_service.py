
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models.database import Reminder
from app.services.llm_service import llm_service

logger = logging.getLogger(__name__)

try:                              # optional dependency, preferred path
    import dateparser
except ImportError:               # pragma: no cover
    dateparser = None
    logger.warning(
        "dateparser is not installed; falling back to model-generated ISO "
        "datetimes only. Run: pip install dateparser"
    )

MAX_CONTENT = 500
TIME_OF_DAY_HOUR = {"morning": 9, "afternoon": 15, "evening": 19, "night": 21}


class ReminderService:
    DEFAULT_HOUR = 9

    async def maybe_create(
        self,
        db: Session,
        user_id: int,
        message: str,
        user_timezone: str = "UTC",
    ) -> Optional[Dict[str, Any]]:
        """
        Returns:
            None                                               -> not a reminder request
            {"status": "needs_time", "content": ...}           -> intent, no usable time
            {"status": "created", "reminder", "local_time"}    -> row written
        """
        if not (message or "").strip():
            return None

        tz = self._safe_tz(user_timezone)
        now_local = datetime.now(tz)

        data = await llm_service.extract(
            instruction=self._instruction(now_local, tz),
            user_message=message,
        )
        if not data:
            logger.warning("Reminder extraction unavailable for user %s", user_id)
            return None
        if not self._truthy(data.get("is_reminder")):
            return None

        content = str(data.get("content") or "").strip().strip('"')[:MAX_CONTENT]
        if len(content) < 2:
            logger.info("Reminder intent with no usable content: %r", data)
            return None

        when_local = self._resolve(data, tz, now_local)
        if when_local is None:
            return {"status": "needs_time", "content": content}

        # Your DateTime columns are not timezone=True and the scheduler compares
        # against naive UTC. Keep the entire DB in naive UTC.
        when_utc = when_local.astimezone(timezone.utc).replace(tzinfo=None)

        existing = (
            db.query(Reminder)
            .filter(
                Reminder.user_id == user_id,
                Reminder.content == content,
                Reminder.scheduled_at == when_utc,
                Reminder.sent_at.is_(None),
            )
            .first()
        )
        if existing:
            logger.info("Duplicate reminder ignored for user %s: %r", user_id, content)
            return {
                "status": "created",
                "reminder": existing,
                "local_time": self._format(when_local),
            }

        reminder = Reminder(user_id=user_id, content=content, scheduled_at=when_utc)
        db.add(reminder)
        db.commit()
        db.refresh(reminder)

        logger.info(
            "Reminder %s created for user %s at %s UTC (%s local)",
            reminder.id, user_id, when_utc, when_local,
        )
        return {
            "status": "created",
            "reminder": reminder,
            "local_time": self._format(when_local),
        }

    # ------------------------------------------------------------- extraction
    @staticmethod
    def _instruction(now_local: datetime, tz: ZoneInfo) -> str:
        return (
            "Decide whether the message below is asking to be reminded of something "
            "at a later time, and if so extract the details.\n\n"
            f"The user's current local date and time is "
            f"{now_local.strftime('%A %d %B %Y, %H:%M')} in timezone {tz}.\n\n"
            "Return ONLY this JSON object, no prose, no code fences:\n"
            '{"is_reminder": true|false, "content": string, "when_phrase": string, '
            '"when_iso": string|null, "precision": "exact"|"date_only"|"none", '
            '"time_of_day": "morning"|"afternoon"|"evening"|"night"|null}\n\n'
            "RULES\n"
            "- is_reminder is true for any request to be told, alerted, woken, nudged "
            "or reminded later, and for any task or event the user says they must not "
            "forget. It is false for questions about reminders in general, for past "
            "events, and for ordinary conversation.\n"
            "- content: a short imperative task in the user's own words, e.g. "
            "'Book tickets for Murree', 'Call mum', 'Take the medicine'. No time inside it.\n"
            "- when_phrase: the time expression copied VERBATIM from the message, e.g. "
            "'tomorrow', 'in 2 hours', 'next Monday morning', 'at 6am'. Empty string if "
            "the message contains no time expression at all.\n"
            "- when_iso: resolve the time expression against the current local date and "
            "time given above and return it as local ISO 8601 without a timezone suffix, "
            "e.g. 2026-09-06T09:00:00. It must be in the FUTURE. Null if there is no "
            "time expression.\n"
            "- precision: 'exact' when a clock time or a relative offset was given "
            "('at 6am', 'in 2 hours'); 'date_only' when only a day was given "
            "('tomorrow', 'next Friday'); 'none' when no time was given at all.\n"
            "- time_of_day: only if they said morning, afternoon, evening or night."
        )

    # -------------------------------------------------------------- resolution
    def _resolve(
        self, data: Dict[str, Any], tz: ZoneInfo, now_local: datetime
    ) -> Optional[datetime]:
        precision = str(data.get("precision") or "").lower()
        phrase = str(data.get("when_phrase") or "").strip()

        if precision == "none" and not phrase:
            return None

        # (a) deterministic parse of the verbatim phrase
        candidate = self._parse_phrase(phrase, tz, now_local)

        # (b) the model's grounded ISO answer
        if candidate is None:
            candidate = self._parse_iso(data.get("when_iso"), tz)

        if candidate is None:
            return None

        # Only a bare date was given: pick a civilised hour instead of "now".
        if precision == "date_only":
            hour = TIME_OF_DAY_HOUR.get(
                str(data.get("time_of_day") or "").lower(), self.DEFAULT_HOUR
            )
            candidate = candidate.replace(hour=hour, minute=0, second=0, microsecond=0)

        candidate = candidate.replace(second=0, microsecond=0)

        # Never schedule in the past. A date_only snap can land behind "now"
        # (asking at 8pm for "today"), so roll it forward a day once.
        if candidate <= now_local:
            if precision == "date_only":
                candidate += timedelta(days=1)
            if candidate <= now_local:
                logger.info("Resolved reminder time %s is in the past; rejecting", candidate)
                return None

        return candidate

    @staticmethod
    def _parse_phrase(
        phrase: str, tz: ZoneInfo, now_local: datetime
    ) -> Optional[datetime]:
        if not phrase or dateparser is None:
            return None
        try:
            result = dateparser.parse(
                phrase,
                settings={
                    "TIMEZONE": str(tz),
                    "TO_TIMEZONE": str(tz),
                    "RETURN_AS_TIMEZONE_AWARE": True,
                    "PREFER_DATES_FROM": "future",
                    "RELATIVE_BASE": now_local.replace(tzinfo=None),
                },
            )
        except Exception:
            logger.exception("dateparser crashed on %r", phrase)
            return None

        if result is None:
            return None
        if result.tzinfo is None:
            result = result.replace(tzinfo=tz)
        return result.astimezone(tz)

    @staticmethod
    def _parse_iso(value: Any, tz: ZoneInfo) -> Optional[datetime]:
        text = str(value or "").strip()
        if not text:
            return None
        text = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            logger.info("Model returned unparseable ISO datetime %r", text)
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz)

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _format(when_local: datetime) -> str:
        return when_local.strftime("%A %d %B at %I:%M %p").replace(" 0", " ")

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "yes", "1")

    @staticmethod
    def _safe_tz(name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name or "UTC")
        except Exception:
            return ZoneInfo("UTC")


reminder_service = ReminderService()