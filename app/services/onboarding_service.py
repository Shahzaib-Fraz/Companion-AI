
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models.database import User
from app.repositories.user_repository import UserRepository
from app.services.llm_service import LLMError, llm_service

logger = logging.getLogger(__name__)

STEP_ORDER: Tuple[str, ...] = ("name", "language", "timezone", "tier", "whatsapp")

# E.164: leading +, country code cannot start with 0, 7-15 digits total.
# Deliberately strict — this column is UNIQUE indexed, so nothing ambiguous
# is allowed anywhere near it.
PHONE_RE = re.compile(r"^\+?[1-9]\d{6,14}$")
LANG_RE = re.compile(r"^[a-z]{2}$")

# What each step is asking for, in words the extractor can use.
STEP_SUBJECT = {
    "name": "the name they want to be called",
    "language": "which language they want to be spoken to in",
    "timezone": "which timezone, city or country they are in",
    "tier": "whether they want the Free plan or the Premium plan",
    "whatsapp": "their WhatsApp number, which they are free to skip",
}

# After this many unusable answers at one step, apply a safe default and move
# on rather than asking forever. In-memory, so it resets on restart — fine for
# an MVP, and the reason a small `onboarding_attempts` column would be better.
MAX_REASKS = 2
SAFE_DEFAULTS = {"language": "en", "timezone": "UTC", "tier": "free"}

# EMERGENCY ONLY. Used when the Groq call raises, never otherwise.
FALLBACK_PROMPTS = {
    "name": "Before we start — what should I call you?",
    "language": "Which language would you like me to use with you?",
    "timezone": "What timezone are you in? A city or country is fine.",
    "tier": "Free or Premium? Premium adds long-term memory across chats.",
    "whatsapp": "Want reminders on WhatsApp too? Send your number, or say skip.",
    "done": "All set. What would you like to talk about?",
}


class OnboardingService:
    def __init__(self):
        self._reasks: Dict[Tuple[int, str], int] = {}

    # ------------------------------------------------------------- step logic
    @staticmethod
    def detect_step(user: User, profile) -> Optional[str]:
        """SINGLE SOURCE OF TRUTH. Returns the step name, or None if complete."""
        if getattr(profile, "onboarding_completed", False):
            return None
        if not profile.display_name:
            return "name"
        if not profile.language:
            return "language"
        if not profile.timezone:
            return "timezone"
        if not profile.account_tier:
            return "tier"
        # phone_number is intentionally NOT consulted: it is optional data,
        # never a control flag.
        return "whatsapp"

    @staticmethod
    def missing_fields(user: User, profile) -> List[str]:
        missing = []
        if not profile.display_name:
            missing.append("name")
        if not profile.language:
            missing.append("language")
        if not profile.timezone:
            missing.append("timezone")
        if not profile.account_tier:
            missing.append("tier")
        if not getattr(user, "phone_number", None):
            missing.append("whatsapp")
        return missing

    # ------------------------------------------------------------------ entry
    async def advance(self, db: Session, user_id: int, message: str) -> Dict[str, Any]:
        """
        Process one onboarding turn.

        Returns {"response", "onboarding_step", "onboarding_completed", "captured"}.
        onboarding_step is None when onboarding just finished.
        """
        user = UserRepository.get_by_id(db, user_id)
        profile = UserRepository.get_or_create_profile(db, user_id)

        step = self.detect_step(user, profile)
        if step is None:
            return {
                "response": await self._say("done", "Onboarding is already finished. Greet them briefly and ask what they would like to talk about.", profile),
                "onboarding_step": None,
                "onboarding_completed": True,
                "captured": {},
            }

        logger.info("Onboarding | user=%s | step=%s | input=%r", user_id, step, message)

        missing = self.missing_fields(user, profile)
        data = await llm_service.extract(
            instruction=self._extract_instruction(step, missing),
            user_message=message,
        )
        if data is None:
            logger.warning("Extraction unavailable for user %s at step %s", user_id, step)
            data = {}

        try:
            captured = self._apply(db, user, profile, data, step)
        except Exception:
            db.rollback()
            logger.exception("Failed to persist onboarding data for user %s", user_id)
            captured = {}

        # Re-read: _apply wrote through the repository.
        user = UserRepository.get_by_id(db, user_id)
        profile = UserRepository.get_or_create_profile(db, user_id)

        # Stuck on the same step with nothing captured? Count it, and after a
        # couple of tries take the safe default so onboarding always finishes.
        forced = None
        if step not in captured and step != "whatsapp":
            key = (user_id, step)
            self._reasks[key] = self._reasks.get(key, 0) + 1
            if self._reasks[key] > MAX_REASKS:
                forced = self._force_default(db, user_id, step, message)
                if forced:
                    captured[step] = forced
                    self._reasks.pop(key, None)
                    profile = UserRepository.get_or_create_profile(db, user_id)
        else:
            self._reasks.pop((user_id, step), None)

        # The whatsapp step is terminal: it always completes onboarding.
        if step == "whatsapp":
            UserRepository.update_profile(db, user_id, onboarding_completed=True)
            profile = UserRepository.get_or_create_profile(db, user_id)
            logger.info("ONBOARDING COMPLETE for user %s", user_id)

        next_step = self.detect_step(user, profile)
        if next_step is None and not getattr(profile, "onboarding_completed", False):
            UserRepository.update_profile(db, user_id, onboarding_completed=True)
            profile = UserRepository.get_or_create_profile(db, user_id)
            logger.info("ONBOARDING COMPLETE for user %s", user_id)

        note = self._note(step, next_step, captured, data, forced)
        response = await self._say(next_step or "done", note, profile)

        return {
            "response": response,
            "onboarding_step": next_step,
            "onboarding_completed": next_step is None,
            "captured": captured,
        }

    async def process_onboarding(
        self, db: Session, user_id: int, message: str, step: str = None
    ):
        """Legacy signature: returns (next_step, response). Prefer advance()."""
        result = await self.advance(db, user_id, message)
        return result["onboarding_step"], result["response"]

    # ------------------------------------------------------------- extraction
    @staticmethod
    def _extract_instruction(asked_step: str, missing: List[str]) -> str:
        return (
            "You are parsing ONE chat message from a person signing up to an AI "
            "companion app. Your job is to pull out any profile details the message "
            "contains.\n"
            f"The assistant's previous message asked them for: {STEP_SUBJECT.get(asked_step, asked_step)}\n"
            f"Profile details still missing: {', '.join(missing) or 'none'}\n\n"
            "Return ONLY this JSON object, no prose, no code fences:\n"
            '{"name": string|null, "language": string|null, "timezone": string|null, '
            '"tier": "free"|"premium"|null, "phone": string|null, '
            '"declined_phone": true|false, "answered": true|false}\n\n'
            "RULES\n"
            "- Fill a field ONLY if this message actually contains that information. "
            "Otherwise null. Never guess, never invent, never carry over a default.\n"
            "- name: the personal name only. Strip lead-ins like 'my name is', "
            "'I am', 'call me'. Never take a name from a greeting, a plan choice, a "
            "city, an email address or a phone number.\n"
            "- language: the ISO 639-1 two-letter lowercase code for the language they "
            "want to be SPOKEN TO in. Infer it from the language name in any spelling "
            "('urdu', 'Bahasa', 'francais' -> ur, id, fr). If they wrote their message "
            "in a language but did not ask for it, still return null.\n"
            "- timezone: a valid IANA timezone database name, e.g. Asia/Karachi, "
            "Asia/Jakarta, Europe/London, America/New_York. Convert any city, region, "
            "country or abbreviation they mention to the correct IANA name yourself. "
            "If they only gave a UTC offset, return a real IANA zone that currently "
            "uses that offset. If the place is ambiguous or you are unsure, return null.\n"
            "- tier: 'premium' only if they clearly pick the paid/premium/pro option, "
            "'free' if they clearly pick the free/basic option, otherwise null.\n"
            "- phone: their phone number in E.164 format, a plus sign then digits only, "
            "no spaces or dashes. Include the country code; if no country code is given "
            "but you know their country from this conversation, add it. If it is not "
            "clearly a phone number, return null.\n"
            "- declined_phone: true if they refuse, skip, postpone, say maybe later, or "
            "say they do not have a number.\n"
            "- answered: true if the message is a real attempt to answer what was asked; "
            "false if it is off-topic, a question back, or nonsense."
        )

    # ------------------------------------------------------------- persistence
    def _apply(
        self,
        db: Session,
        user: User,
        profile,
        data: Dict[str, Any],
        step: str,
    ) -> Dict[str, Any]:
        """
        Validate every candidate value and write only what is storable.
        Returns {field: stored_value} for what was actually saved.
        """
        captured: Dict[str, Any] = {}
        updates: Dict[str, Any] = {}

        # --- name -------------------------------------------------------
        if not profile.display_name:
            name = self._clean_name(data.get("name"))
            if name:
                updates["display_name"] = name
                captured["name"] = name

        # --- language ---------------------------------------------------
        if not profile.language:
            lang = (str(data.get("language") or "")).strip().lower()
            if LANG_RE.match(lang):
                updates["language"] = lang
                captured["language"] = lang
            elif lang:
                logger.info("Rejected language candidate %r for user %s", lang, user.id)

        # --- timezone ---------------------------------------------------
        if not profile.timezone:
            tz = self._clean_timezone(data.get("timezone"))
            if tz:
                updates["timezone"] = tz
                captured["timezone"] = tz
            elif data.get("timezone"):
                logger.info(
                    "Rejected timezone candidate %r for user %s",
                    data.get("timezone"), user.id,
                )

        # --- tier -------------------------------------------------------
        if not profile.account_tier:
            tier = (str(data.get("tier") or "")).strip().lower()
            if tier in ("free", "premium"):
                updates["account_tier"] = tier
                captured["tier"] = tier

        if updates:
            UserRepository.update_profile(db, user.id, **updates)
            logger.info("Onboarding saved %s for user %s", updates, user.id)

        # --- phone ------------------------------------------------------
        # Only touched at the whatsapp step, and only ever set to a validated
        # number or left as NULL. A sentinel string in a unique column is what
        # caused the old UniqueViolation.
        if step == "whatsapp" and not getattr(user, "phone_number", None):
            phone = self._clean_phone(data.get("phone"))
            if phone and not self._phone_taken(db, phone, user.id):
                user.phone_number = phone
                db.commit()
                captured["whatsapp"] = phone
                logger.info("WhatsApp linked for user %s", user.id)
            elif phone:
                logger.warning(
                    "Phone %s already linked to another account; skipping for user %s",
                    phone, user.id,
                )
                captured["whatsapp_conflict"] = True
            elif data.get("declined_phone"):
                captured["whatsapp_declined"] = True

        return captured

    def _force_default(
        self, db: Session, user_id: int, step: str, message: str
    ) -> Optional[str]:
        """Last resort so onboarding cannot loop forever on one question."""
        if step == "name":
            # They typed something; take it verbatim rather than asking a
            # fourth time. Better a slightly odd display name than a dead flow.
            candidate = (message or "").strip()[:100]
            if len(candidate) >= 2:
                UserRepository.update_profile(db, user_id, display_name=candidate)
                logger.warning("Forced display_name=%r for user %s", candidate, user_id)
                return candidate
            return None

        value = SAFE_DEFAULTS.get(step)
        if not value:
            return None
        field = {"language": "language", "timezone": "timezone", "tier": "account_tier"}[step]
        UserRepository.update_profile(db, user_id, **{field: value})
        logger.warning("Forced %s=%s for user %s after repeated unusable answers", field, value, user_id)
        return value

    # --------------------------------------------------------------- validators
    @staticmethod
    def _clean_name(value: Any) -> Optional[str]:
        name = str(value or "").strip().strip(".,!\"'")
        if len(name) < 2 or len(name) > 100:
            return None
        if name.isdigit() or "@" in name:
            return None
        return name

    @staticmethod
    def _clean_timezone(value: Any) -> Optional[str]:
        tz = str(value or "").strip()
        if not tz or len(tz) > 50:  # column is String; keep it sane
            return None
        try:
            ZoneInfo(tz)          # the only reliable validity test
        except Exception:
            return None
        return tz

    @staticmethod
    def _clean_phone(value: Any) -> Optional[str]:
        raw = str(value or "")
        cleaned = re.sub(r"[\s\-().]", "", raw)
        if not PHONE_RE.match(cleaned):
            return None
        return cleaned[:20]        # column is String(20)

    @staticmethod
    def _phone_taken(db: Session, phone: str, user_id: int) -> bool:
        return (
            db.query(User)
            .filter(User.phone_number == phone, User.id != user_id)
            .first()
            is not None
        )

    # -------------------------------------------------------------- responses
    @staticmethod
    def _note(
        step: str,
        next_step: Optional[str],
        captured: Dict[str, Any],
        data: Dict[str, Any],
        forced: Optional[str],
    ) -> str:
        """
        Instruction for Groq describing what to say next. This is the prompt,
        not the reply — the sentence the user reads is always generated.
        """
        parts: List[str] = []

        if "name" in captured:
            parts.append(f"You just learned their name is {captured['name']}; greet them by it once.")
        if "language" in captured:
            parts.append(f"They want to be spoken to in the language with ISO code '{captured['language']}'; use it from now on.")
        if "timezone" in captured:
            parts.append(f"Their timezone is {captured['timezone']}; confirm it in a few words.")
        if "tier" in captured:
            parts.append(f"They chose the {captured['tier']} plan; confirm it briefly.")
        if "whatsapp" in captured:
            parts.append("Their WhatsApp number is now linked; confirm that warmly.")
        if captured.get("whatsapp_conflict"):
            parts.append("That number is already linked to another account so WhatsApp was skipped; say support can merge accounts.")
        if captured.get("whatsapp_declined"):
            parts.append("They skipped WhatsApp; say that is completely fine and reminders will arrive by email.")

        if forced and step in ("language", "timezone", "tier"):
            parts.append(
                f"Their answer was still unclear, so {step} was set to '{forced}' for now; "
                "mention in passing they can change it later, without making it a big deal."
            )
        elif step not in captured and step == next_step:
            parts.append(
                f"They did not give a usable answer for {STEP_SUBJECT.get(step, step)}. "
                "Do not repeat the question word for word — rephrase it more simply and "
                "give one short example of a valid answer."
            )

        if next_step is None:
            parts.append(
                "Onboarding is now finished. Do not ask any more setup questions. "
                "Tell them briefly that they can also ask you to remind them about "
                "things and you will email them at the right time, then ask what they "
                "would like to talk about."
            )
        elif next_step != step:
            parts.append(f"Now ask them for {STEP_SUBJECT[next_step]}.")
            if next_step == "tier":
                parts.append(
                    "Explain the difference in one clause: Free is normal chat, Premium "
                    "remembers things across conversations."
                )
            if next_step == "whatsapp":
                parts.append("Make it clear that skipping is completely fine.")

        return " ".join(parts)

    @staticmethod
    async def _say(step: str, note: str, profile) -> str:
        language = (profile.language or "en") if profile else "en"
        name = (getattr(profile, "display_name", None) or "").strip()

        system = (
            "You are a warm AI companion walking someone through a short chat-based "
            "setup. You are mid-conversation, so do not greet them again unless you "
            "have just learned their name.\n"
            "Write one or two short sentences of plain conversational prose. No lists, "
            "no bullet points, no markdown, no emoji spam, no restating instructions.\n"
            "Ask at most ONE question, and only the one you are told to ask.\n"
            f"Reply in the language with ISO 639-1 code '{language}'."
        )
        if name:
            system += f"\nTheir name is {name}."

        try:
            return await llm_service.chat(
                system=system,
                user=f"Say this to them now: {note}",
                temperature=0.6,
                max_tokens=200,
            )
        except LLMError:
            logger.exception("Onboarding phrasing call failed at step %r", step)
            return FALLBACK_PROMPTS.get(step, FALLBACK_PROMPTS["done"])


onboarding_service = OnboardingService()