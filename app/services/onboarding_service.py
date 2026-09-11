"""
Onboarding Service

Step is derived from which profile fields are already populated
(display_name, timezone, language, account_tier) rather than a
persisted step pointer, since UserProfile has no onboarding_step
column. This makes onboarding naturally resumable across requests
with zero schema changes.

Parsing is LLM-first: for tier/name/timezone/language, a single LLM
call extracts BOTH the structured value from free text (e.g. "I'm in
Pakistan" -> Asia/Karachi) AND a short natural acknowledgment line
(e.g. "Got it — your timezone is set to Asia/Karachi!"), so plain
chit-chat like "hi" gets a natural reply instead of a robotic error,
and successful answers get an explicit confirmation of what was
captured before the next question. If the LLM is unavailable or
returns something unusable, each step falls back to a strict parser
and a static acknowledgment template so onboarding never fully
breaks during an LLM outage. WhatsApp phone parsing stays
regex-only — it's pure format validation, not language understanding.
"""
import json
import logging
from typing import Optional, Tuple
from zoneinfo import available_timezones

from app.schemas.schemas import PIIMasker
from app.services.llm_service import LLMError, llm_service

logger = logging.getLogger(__name__)


class OnboardingService:
    """
    Handles onboarding flow for new users.
    Email is collected during signup, chat collects: tier → name → timezone → language → whatsapp
    """

    # Onboarding steps (email already done via signup form)
    STEP_TIER = 0         # Free vs Premium
    STEP_NAME = 1         # Display name
    STEP_TIMEZONE = 2     # Timezone
    STEP_LANGUAGE = 3     # Language
    STEP_WHATSAPP = 4     # WhatsApp phone (optional)
    STEP_COMPLETE = 5     # Done

    STEP_NAMES = {
        0: "tier",
        1: "name",
        2: "timezone",
        3: "language",
        4: "whatsapp",
        5: "complete",
    }

    # What each step is trying to extract, used inside the shared LLM prompt template.
    _STEP_TASKS = {
        STEP_TIER: (
            'Extract which account tier the user wants: "free" or "premium". '
            'Handle indirect phrasing (e.g. "the paid one" = premium, "just the basic option" = free).'
        ),
        STEP_NAME: (
            "Extract the person's display name. Handle phrasing like "
            '"call me X", "I\'m X", "my name is X", or a bare name.'
        ),
        STEP_TIMEZONE: (
            "Extract where they live or their timezone, in ANY form (country, city, "
            "region, or timezone name), and convert it to a single valid IANA timezone "
            "identifier (e.g. Asia/Karachi, America/New_York, Europe/London, "
            "Australia/Sydney). If they name a country, use its most representative "
            "timezone."
        ),
        STEP_LANGUAGE: (
            'Extract their preferred language (e.g. "English", "Urdu", "Spanish").'
        ),
    }

    def get_step_prompt(self, step: int) -> str:
        """Fallback / next-question prompt for a given onboarding step."""
        prompts = {
            self.STEP_TIER: "Choose your tier: Free or Premium?",
            self.STEP_NAME: "What's your name?",
            self.STEP_TIMEZONE: "What's your timezone? (e.g., Asia/Karachi, or just tell me your city/country)",
            self.STEP_LANGUAGE: "Preferred language? (English, Urdu, etc.)",
            self.STEP_WHATSAPP: "Add WhatsApp phone? (optional, reply 'skip')",
        }
        return prompts.get(step, "Continue?")

    @staticmethod
    def _fallback_ack(step: int, value: str) -> str:
        """Static confirmation text used when the LLM can't produce one."""
        if step == OnboardingService.STEP_TIER:
            return f"Got it — your tier is set to {value.capitalize()}."
        if step == OnboardingService.STEP_NAME:
            return f"Nice to meet you, {value}!"
        if step == OnboardingService.STEP_TIMEZONE:
            return f"Got it — your timezone is set to {value}."
        if step == OnboardingService.STEP_LANGUAGE:
            return f"Got it — your language is set to {value}."
        return "Got it!"

    # ========== STEP DERIVATION ==========

    def get_current_step(self, profile, user) -> int:
        """
        Derive the current onboarding step from actual saved data
        instead of a persisted step pointer, since no such column
        exists on UserProfile. Checked in collection order:
        tier → name → timezone → language → whatsapp.
        """
        if not profile.account_tier:
            return self.STEP_TIER
        if not profile.display_name:
            return self.STEP_NAME
        if not profile.timezone:
            return self.STEP_TIMEZONE
        if not profile.language:
            return self.STEP_LANGUAGE
        return self.STEP_WHATSAPP

    # ========== LLM-FIRST PARSING + ACKNOWLEDGMENT ==========

    @staticmethod
    def _is_valid_timezone(tz: str) -> bool:
        return tz in available_timezones()

    async def _llm_extract(self, step: int, user_input: str) -> Optional[dict]:
        """
        Ask the LLM to, in one call: (1) extract a structured value from
        free text if the message actually answers the current step's
        question, and (2) write a short natural acknowledgment/reply line
        — confirming the captured value if there is one, or just
        responding naturally (e.g. to a greeting or off-topic message) if
        not. Returns None if the LLM is down, errors, or returns
        unparseable output — callers fall back to strict parsing +
        static acknowledgment text in that case.
        """
        task = self._STEP_TASKS.get(step)
        if not task:
            return None

        system_prompt = (
            "You are a friendly onboarding assistant chatting with a new user. "
            f"Current step's task: {task}\n\n"
            "Read the user's message and respond with ONLY this JSON "
            '(no markdown, no code fences, no explanation):\n'
            '{"value": <extracted value as a string, or null if the message '
            'does not actually answer the question>, '
            '"acknowledgment": "<one short, warm, natural sentence>"}\n\n'
            "Rules for \"acknowledgment\":\n"
            "- If value is NOT null, explicitly confirm what was captured "
            '(e.g. "Got it — your timezone is set to Asia/Karachi!").\n'
            "- If value IS null (e.g. the user said something unrelated like "
            '"hi", asked a question, or was ambiguous), respond naturally and '
            "briefly to what they actually said — do NOT confirm any value, "
            "and do NOT sound like an error message.\n"
            "- Never invent or ask the next onboarding question yourself — "
            "the calling system appends that separately."
        )

        try:
            raw = await llm_service.chat(
                system=system_prompt,
                messages=[{"role": "user", "content": user_input}],
            )
        except LLMError:
            logger.warning(
                "LLM unavailable for onboarding parse, falling back to strict parser",
                extra={"step": self.STEP_NAMES.get(step, "?")}
            )
            return None

        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned

        try:
            parsed = json.loads(cleaned)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "LLM returned non-JSON for onboarding parse, falling back",
                extra={"step": self.STEP_NAMES.get(step, "?"), "raw": raw}
            )
            return None

        if not isinstance(parsed, dict):
            return None

        value = parsed.get("value")
        if isinstance(value, str) and not value.strip():
            value = None

        ack = parsed.get("acknowledgment")
        if not isinstance(ack, str) or not ack.strip():
            ack = None

        return {"value": (str(value).strip() if value is not None else None), "acknowledgment": ack}

    async def parse_step(self, step: int, user_input: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Parse user input for tier/name/timezone/language via the LLM
        first, falling back to strict parsing + a static acknowledgment
        if the LLM can't produce a usable value.

        Returns (value, acknowledgment):
          - value: the captured field value, or None if this message
            didn't answer the question (e.g. a greeting or gibberish).
          - acknowledgment: a natural reply line. Never None — falls
            back to a static line if the LLM didn't provide one.

        STEP_WHATSAPP is NOT handled here — call parse_whatsapp_phone()
        directly for that step.
        """
        if step == self.STEP_WHATSAPP:
            raise ValueError("Use parse_whatsapp_phone() for STEP_WHATSAPP")

        llm_result = await self._llm_extract(step, user_input)
        llm_value = llm_result["value"] if llm_result else None
        llm_ack = llm_result["acknowledgment"] if llm_result else None

        value: Optional[str] = None

        if step == self.STEP_TIER:
            if llm_value in ("free", "premium"):
                value = llm_value
                logger.info("✅ Tier selected via LLM", extra={"tier": value})
            else:
                value = self.parse_tier(user_input)

        elif step == self.STEP_NAME:
            if llm_value:
                value = self.parse_name(llm_value)
            if not value:
                value = self.parse_name(user_input)

        elif step == self.STEP_TIMEZONE:
            if llm_value and self._is_valid_timezone(llm_value):
                value = llm_value
                logger.info("✅ Timezone set via LLM", extra={"timezone": value})
            else:
                if llm_value:
                    logger.warning(
                        "LLM returned an invalid IANA timezone, falling back",
                        extra={"llm_value": llm_value}
                    )
                value = self.parse_timezone(user_input)

        elif step == self.STEP_LANGUAGE:
            if llm_value:
                value = self.parse_language(llm_value)
            if not value:
                value = self.parse_language(user_input)

        # Decide the acknowledgment to use
        if value:
            # Prefer the LLM's acknowledgment only if it actually came from
            # a turn where the LLM itself recognized a value (avoids using
            # an LLM "clarifying" ack on a value we only got via fallback parsing).
            if llm_ack and llm_value:
                acknowledgment = llm_ack
            else:
                acknowledgment = self._fallback_ack(step, value)
        else:
            acknowledgment = llm_ack or "Sorry, I didn't quite catch that."

        return value, acknowledgment

    # ========== STRICT FALLBACK PARSERS (used when LLM is unavailable) ==========

    def parse_tier(self, user_input: str) -> Optional[str]:
        """Fallback: parse tier from exact keywords 'free'/'premium'."""
        tier_input = user_input.lower().strip()

        if "premium" in tier_input:
            tier = "premium"
        elif "free" in tier_input:
            tier = "free"
        else:
            logger.info(
                "❌ Tier validation failed (fallback)",
                extra={
                    "step": self.STEP_NAMES[self.STEP_TIER],
                    "reason": "unrecognized_tier",
                }
            )
            return None

        logger.info(
            "✅ Tier selected (fallback)",
            extra={
                "tier": tier,
                "step": self.STEP_NAMES[self.STEP_TIER],
            }
        )
        return tier

    def parse_name(self, user_input: str) -> Optional[str]:
        """Fallback: treat the whole trimmed input as the name."""
        name = user_input.strip()

        if not name or len(name) < 2:
            logger.info(
                "❌ Name validation failed (fallback)",
                extra={
                    "step": self.STEP_NAMES[self.STEP_NAME],
                    "reason": "name_too_short",
                }
            )
            return None

        logger.info(
            "✅ Name parsed (fallback)",
            extra={
                "name_masked": PIIMasker.mask_name(name),
                "step": self.STEP_NAMES[self.STEP_NAME],
            }
        )
        return name

    def parse_timezone(self, user_input: str) -> Optional[str]:
        """Fallback: require a strict Region/City IANA-format string."""
        timezone = user_input.strip()

        if "/" not in timezone or len(timezone) < 5 or not self._is_valid_timezone(timezone):
            logger.info(
                "❌ Timezone validation failed (fallback)",
                extra={
                    "step": self.STEP_NAMES[self.STEP_TIMEZONE],
                    "reason": "invalid_format_or_unknown_tz",
                }
            )
            return None

        logger.info(
            "✅ Timezone set (fallback)",
            extra={
                "timezone": timezone,
                "step": self.STEP_NAMES[self.STEP_TIMEZONE],
            }
        )
        return timezone

    def parse_language(self, user_input: str) -> Optional[str]:
        """Fallback: treat the whole trimmed/lowered input as the language."""
        language = user_input.lower().strip()

        if len(language) < 2:
            logger.info(
                "❌ Language validation failed (fallback)",
                extra={
                    "step": self.STEP_NAMES[self.STEP_LANGUAGE],
                    "reason": "language_too_short",
                }
            )
            return None

        logger.info(
            "✅ Language set (fallback)",
            extra={
                "language": language,
                "step": self.STEP_NAMES[self.STEP_LANGUAGE],
            }
        )
        return language

    def parse_whatsapp_phone(self, user_input: str) -> Optional[str]:
        """Parse WhatsApp phone number from user input (format-only, no LLM needed)."""
        import re

        phone_input = user_input.strip()

        # Check if user wants to skip
        if phone_input.lower() in ("skip", "no", "none", "-"):
            logger.info(
                "ℹ️  WhatsApp phone skipped",
                extra={"step": self.STEP_NAMES[self.STEP_WHATSAPP]}
            )
            return None  # Signal: skipped

        # Basic phone validation
        phone_pattern = r"^\+?[0-9]{10,15}$"
        if not re.match(phone_pattern, phone_input.replace(" ", "")):
            logger.info(
                "❌ WhatsApp phone validation failed",
                extra={
                    "step": self.STEP_NAMES[self.STEP_WHATSAPP],
                    "reason": "invalid_format",
                }
            )
            return ""  # Signal: invalid (continue asking)

        phone = phone_input.replace(" ", "")

        logger.info(
            "✅ WhatsApp phone parsed",
            extra={
                "phone_masked": PIIMasker.mask_phone(phone),
                "step": self.STEP_NAMES[self.STEP_WHATSAPP],
            }
        )
        return phone

    def get_next_step(self, current_step: int) -> int:
        """Get the next onboarding step."""
        return current_step + 1

    def is_complete(self, step: int) -> bool:
        """Check if onboarding is complete."""
        return step >= self.STEP_COMPLETE


# Global instance
onboarding_service = OnboardingService()