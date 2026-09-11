"""
Chat Service - Minimal fix for onboarding
Uses existing parsing methods from OnboardingService
Email already collected via signup form

Onboarding step is derived from which profile fields are already
populated (see OnboardingService.get_current_step) instead of a
persisted onboarding_step pointer, since UserProfile has no such
column. WhatsApp phone is stored on User.phone_number, the only
real phone field on the model — User has no whatsapp_phone column.

Onboarding answers for tier/name/timezone/language are parsed via
OnboardingService.parse_step(), which is LLM-first: one LLM call
extracts the value AND writes a natural acknowledgment line, so a
plain "hi" gets a friendly reply instead of a generic error, and a
successful answer gets an explicit confirmation (e.g. "your timezone
is set to Asia/Karachi") before the next question. Falls back to a
strict parser + static acknowledgment if the LLM is unavailable.
"""
import asyncio
import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.repositories.message_repository import MessageRepository
from app.repositories.user_repository import UserRepository
from app.services.context_builder import context_builder
from app.services.embedding_service import embedding_service
from app.services.llm_service import LLMError, llm_service
from app.services.memory_service import memory_service
from app.services.onboarding_service import onboarding_service
from app.services.reminder_service import reminder_service
from app.core.config import settings

logger = logging.getLogger(__name__)

LLM_DOWN = "I'm having trouble thinking straight for a second — could you send that again?"


class ChatService:
    """Chat service with minimal onboarding orchestration."""

    @staticmethod
    def _extract_durable_facts(user_message: str) -> Dict[str, Any]:
        """Extract durable facts from user message."""
        if not user_message or not user_message.strip():
            return {"facts": []}

        facts = []
        msg_lower = user_message.lower()

        # Extract name
        if "my name is" in msg_lower or "call me" in msg_lower:
            name = ChatService._extract_name(user_message)
            if name:
                facts.append({
                    "fact": f"User's name is {name}",
                    "category": "name",
                    "confidence": 0.95,
                })

        # Extract location
        if "work from" in msg_lower or "live in" in msg_lower or "based in" in msg_lower:
            location = ChatService._extract_location(user_message)
            if location:
                facts.append({
                    "fact": f"User works/lives in {location}",
                    "category": "location",
                    "confidence": 0.85,
                })

        # Extract preferences
        if "prefer" in msg_lower or "like" in msg_lower or "enjoy" in msg_lower:
            prefs = ChatService._extract_preferences(user_message)
            facts.extend(prefs)

        return {"facts": facts}

    @staticmethod
    def _extract_name(text: str) -> Optional[str]:
        """Extract name from phrases like 'my name is John'."""
        for kw in ["my name is", "call me"]:
            if kw in text.lower():
                parts = text.lower().split(kw)
                if len(parts) > 1:
                    name = parts[1].strip().split()[0]
                    if len(name) > 2 and name.isalpha():
                        return name
        return None

    @staticmethod
    def _extract_location(text: str) -> Optional[str]:
        """Extract location from phrases like 'I live in New York'."""
        for kw in ["work from", "live in", "based in"]:
            if kw in text.lower():
                parts = text.lower().split(kw)
                if len(parts) > 1:
                    location = " ".join(parts[1].strip().split()[0:2])
                    return location
        return None

    @staticmethod
    def _extract_preferences(text: str) -> list:
        """Extract preferences from user message."""
        prefs = []
        msg_lower = text.lower()

        if "prefer" in msg_lower or "like" in msg_lower:
            if "evening" in msg_lower or "morning" in msg_lower or "afternoon" in msg_lower:
                prefs.append({
                    "fact": "User prefers certain times of day for communication",
                    "category": "preference",
                    "confidence": 0.75,
                })
        return prefs

    # ============================================================================
    # MAIN ENTRY POINT
    # ============================================================================
    async def process_message(
        self,
        db: Session,
        message: str,
        user_id: Optional[int] = None,
        channel: str = "web",
    ) -> Dict[str, Any]:
        """Main entry point for processing user messages."""
        message = (message or "").strip()
        if not message:
            return {"error": "Empty message"}

        if user_id is None:
            return {"error": "Authentication required"}

        user = UserRepository.get_by_id(db, user_id)
        if not user:
            return {"error": "User not found"}

        profile = UserRepository.get_or_create_profile(db, user_id)

        # Email is already collected from signup, so start at STEP_TIER
        if not profile.onboarding_completed:
            return await self._handle_onboarding(db, user_id, message, channel, profile)

        return await self._handle_regular_chat(db, user_id, message, channel)

    # ============================================================================
    # ONBOARDING - step derived from populated profile fields, not a stored pointer.
    # tier/name/timezone/language parsed + acknowledged via LLM (with strict fallback);
    # whatsapp parsed via format-only regex.
    # ============================================================================
    async def _handle_onboarding(
        self, db: Session, user_id: int, message: str, channel: str, profile
    ) -> Dict[str, Any]:
        """
        Handle onboarding flow.
        Email is already collected from signup form.
        Collects: tier → name → timezone → language → whatsapp

        Step is derived each turn from which profile fields are already
        filled (see OnboardingService.get_current_step), so this is
        naturally resumable across requests with no onboarding_step
        column required.
        """
        user = UserRepository.get_by_id(db, user_id)
        if not user:
            return {"error": "User not found"}

        current_step = onboarding_service.get_current_step(profile, user)

        captured = {}
        parsed_value: Optional[str] = None
        acknowledgment: Optional[str] = None
        completed = False

        if current_step == onboarding_service.STEP_TIER:
            parsed_value, acknowledgment = await onboarding_service.parse_step(current_step, message)
            if parsed_value:
                captured["tier"] = parsed_value
                profile.account_tier = parsed_value

        elif current_step == onboarding_service.STEP_NAME:
            parsed_value, acknowledgment = await onboarding_service.parse_step(current_step, message)
            if parsed_value:
                captured["name"] = parsed_value
                profile.display_name = parsed_value

        elif current_step == onboarding_service.STEP_TIMEZONE:
            parsed_value, acknowledgment = await onboarding_service.parse_step(current_step, message)
            if parsed_value:
                captured["timezone"] = parsed_value
                profile.timezone = parsed_value

        elif current_step == onboarding_service.STEP_LANGUAGE:
            parsed_value, acknowledgment = await onboarding_service.parse_step(current_step, message)
            if parsed_value:
                captured["language"] = parsed_value
                profile.language = parsed_value

        elif current_step == onboarding_service.STEP_WHATSAPP:
            # Format-only validation, no NLU needed here.
            # None -> user skipped, "" -> invalid format (ask again),
            # non-empty str -> valid phone
            parsed_value = onboarding_service.parse_whatsapp_phone(message)
            if parsed_value:
                captured["whatsapp_phone"] = parsed_value
                user.phone_number = parsed_value  # only real phone field on User
                acknowledgment = "Got it — your WhatsApp number is set."
            elif parsed_value is None:
                acknowledgment = "No problem, skipping WhatsApp."
            else:
                acknowledgment = "That doesn't look like a valid phone number."
            if parsed_value is None or parsed_value:
                completed = True
                profile.onboarding_completed = True

        # ✅ Compose response: natural acknowledgment + the real next/current
        # question (question wording always comes from code, never the LLM,
        # so it can't drift or hallucinate a wrong step).
        if current_step == onboarding_service.STEP_WHATSAPP:
            if completed:
                response = f"{acknowledgment} 🎉 Welcome! Onboarding complete. You can now start chatting!"
            else:
                response = f"{acknowledgment} {onboarding_service.get_step_prompt(current_step)}"
        else:
            if parsed_value is None:
                # Didn't answer the question (greeting, off-topic, gibberish, etc.)
                # -> natural acknowledgment + repeat the SAME question.
                ack = acknowledgment or "Sorry, I didn't quite catch that."
                response = f"{ack} {onboarding_service.get_step_prompt(current_step)}"
            else:
                # Answered successfully -> natural confirmation + the NEXT question.
                next_step = onboarding_service.get_current_step(profile, user)
                ack = acknowledgment or "Got it!"
                response = f"{ack} {onboarding_service.get_step_prompt(next_step)}"

        # ✅ Save profile + user
        try:
            db.add(profile)
            db.add(user)
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to save profile: {e}", extra={"user_id": user_id})
            response = "Sorry, there was an error. Please try again."

        # ✅ Persist messages to conversation
        conversation = MessageRepository.get_or_create_conversation(db, user_id, channel)
        self._persist(db, conversation.id, user_id, message, response, channel)

        logger.info(
            "Onboarding turn | user=%s | step=%s | captured=%s | completed=%s",
            user_id, onboarding_service.STEP_NAMES.get(current_step, '?'),
            captured, completed
        )

        return {
            "response": response,
            "onboarding_completed": completed,
        }

    # ============================================================================
    # REGULAR CHAT
    # ============================================================================
    async def _handle_regular_chat(
        self, db: Session, user_id: int, message: str, channel: str
    ) -> Dict[str, Any]:
        """Handle regular chat after onboarding is complete."""
        profile = UserRepository.get_or_create_profile(db, user_id)
        conversation = MessageRepository.get_or_create_conversation(db, user_id, channel)

        saved = MessageRepository.create(db, conversation.id, user_id, "user", message, channel)
        saved_id = getattr(saved, "id", None)

        # --- reminder ingestion ---
        reminder_result = None
        try:
            reminder_result = await reminder_service.maybe_create(
                db, user_id, message, profile.timezone or "UTC"
            )
        except Exception:
            db.rollback()
            logger.exception("Reminder ingestion failed for user %s", user_id)

        system, messages = await context_builder.build(
            db, user_id, message, exclude_message_id=saved_id
        )
        system += self._reminder_fact(reminder_result, profile)

        try:
            llm_response = await llm_service.chat(system=system, messages=messages)
        except LLMError:
            logger.exception("Groq failed on a chat turn for user %s", user_id)
            return {"response": LLM_DOWN, "error": "llm_unavailable", "reminder_set": False}

        MessageRepository.create(db, conversation.id, user_id, "assistant", llm_response, channel)

        if (profile.account_tier or "") == "premium":
            await self._remember(user_id, message, llm_response)

        return {
            "response": llm_response,
            "reminder_set": bool(reminder_result and reminder_result["status"] == "created"),
        }

    @staticmethod
    def _reminder_fact(reminder_result: Optional[Dict[str, Any]], profile) -> str:
        """Get reminder system fact for LLM."""
        if not reminder_result:
            return ""

        tz = profile.timezone or "UTC"

        if reminder_result["status"] == "created":
            content = reminder_result["reminder"].content
            return (
                "\n\nSYSTEM FACT — this already happened, treat it as done:\n"
                f'A reminder has been saved: "{content}", scheduled for '
                f"{reminder_result['local_time']} ({tz}). It will be emailed automatically. "
                "Confirm it warmly in one or two sentences."
            )

        if reminder_result["status"] == "needs_time":
            return (
                "\n\nSYSTEM FACT — this already happened, treat it as done:\n"
                f'They want a reminder for "{reminder_result["content"]}" but gave no time. '
                "Ask them what date and time they want it, in one sentence."
            )

        return ""

    @staticmethod
    async def _remember(user_id: int, message: str, llm_response: str) -> None:
        """Extract and store durable facts from user message."""
        if not settings.ENABLE_MEMORY_EXTRACTION:
            return

        try:
            extraction_result = ChatService._extract_durable_facts(message)
            facts = extraction_result.get("facts", [])

            if not facts:
                logger.info("ℹ️  No durable facts extracted", extra={"user_id": user_id})
                return

            threshold = settings.MEMORY_EXTRACTION_CONFIDENCE_THRESHOLD
            filtered_facts = [f for f in facts if f["confidence"] >= threshold]

            if not filtered_facts:
                logger.info(f"⚠️  All facts below confidence threshold {threshold}", extra={"user_id": user_id})
                return

            for fact in filtered_facts:
                embedding_text = f"[user_stated] {fact['fact']}"

                try:
                    vector = await embedding_service.embed(embedding_text)
                    await memory_service.store_memory(user_id, embedding_text, vector)
                    logger.info(
                        "💾 Stored memory",
                        extra={
                            "user_id": user_id,
                            "category": fact["category"],
                            "confidence": fact["confidence"]
                        }
                    )
                except Exception as e:
                    logger.error(f"Failed to store fact: {e}", extra={"user_id": user_id})

        except Exception:
            logger.exception("Memory extraction failed for user %s", user_id)

    # ============================================================================
    # HELPERS
    # ============================================================================
    @staticmethod
    def _persist(
        db: Session,
        conversation_id: int,
        user_id: int,
        user_message: str,
        assistant_message: str,
        channel: str,
    ) -> None:
        """Save messages to conversation."""
        try:
            MessageRepository.create(db, conversation_id, user_id, "user", user_message, channel)
            MessageRepository.create(db, conversation_id, user_id, "assistant", assistant_message, channel)
        except Exception:
            db.rollback()
            logger.exception("Could not persist messages for user %s", user_id)


chat_service = ChatService()