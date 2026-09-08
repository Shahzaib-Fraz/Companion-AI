
import logging
import re
from typing import Any, Dict, Optional

from email_validator import EmailNotValidError, validate_email
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.repositories.message_repository import MessageRepository
from app.repositories.user_repository import UserRepository
from app.services.context_builder import context_builder
from app.services.embedding_service import embedding_service
from app.services.llm_service import LLMError, llm_service
from app.services.memory_service import memory_service
from app.services.onboarding_service import onboarding_service
from app.services.reminder_service import reminder_service

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Shown only if Groq itself is unreachable. Everything else is generated.
LLM_DOWN = "I'm having trouble thinking straight for a second — could you send that again?"


class ChatService:
    # ------------------------------------------------------------------ router
    async def process_message(
        self,
        db: Session,
        message: str,
        user_id: Optional[int] = None,
        channel: str = "web",
    ) -> Dict[str, Any]:
        message = (message or "").strip()
        if not message:
            return {"error": "Empty message"}

        if user_id is None:
            return await self._handle_email_collection(db, message, channel)

        user = UserRepository.get_by_id(db, user_id)
        if not user:
            return {"error": "User not found"}

        profile = UserRepository.get_or_create_profile(db, user_id)

        if not profile.onboarding_completed:
            return await self._handle_onboarding(db, user_id, message, channel)

        return await self._handle_regular_chat(db, user_id, message, channel)

    # ------------------------------------------------------- email collection
    async def _handle_email_collection(
        self, db: Session, message: str, channel: str
    ) -> Dict[str, Any]:
        email = self._extract_email(message)

        if not email:
            return {
                "response": await self._say(
                    "This is their very first message and it does not contain an email "
                    "address. Introduce yourself in one short sentence and ask for their "
                    "email address so you can create or find their account. Warm, not formal."
                ),
                "onboarding_step": "email",
            }

        user = UserRepository.get_by_email(db, email)

        if user:
            profile = UserRepository.get_or_create_profile(db, user.id)
            token = create_access_token(user.id)
            conversation = MessageRepository.get_or_create_conversation(db, user.id, channel)

            if profile.onboarding_completed:
                system = context_builder.system_prompt(profile)
                note = (
                    "They have just signed back in with their email. Welcome them back "
                    "warmly in one or two sentences and ask what they would like to talk "
                    "about. Do not ask any setup questions."
                )
                response = await self._say(note, system=system)
                self._persist(db, conversation.id, user.id, message, response, channel)
                return {
                    "response": response,
                    "access_token": token,
                    "user_id": user.id,
                    "onboarding_completed": True,
                }

            # Known email, setup never finished: resume where they stopped.
            step = onboarding_service.detect_step(user, profile)
            note = (
                "They are signing back in with their email but never finished setup. "
                f"Welcome them back in one short sentence, then ask them for "
                f"{self._subject(step)}."
            )
            response = await self._say(note, profile=profile)
            self._persist(db, conversation.id, user.id, message, response, channel)
            return {
                "response": response,
                "access_token": token,
                "user_id": user.id,
                "onboarding_step": step,
                "onboarding_completed": False,
            }

        # New account.
        logger.info("Creating new user for %s", email)
        new_user = UserRepository.create(db, email)
        UserRepository.get_or_create_profile(db, new_user.id)
        token = create_access_token(new_user.id)
        conversation = MessageRepository.get_or_create_conversation(db, new_user.id, channel)

        response = await self._say(
            "Their account has just been created. Say so in a few words and ask what "
            "they would like you to call them. One short question only."
        )
        self._persist(db, conversation.id, new_user.id, message, response, channel)

        return {
            "response": response,
            "access_token": token,
            "user_id": new_user.id,
            "onboarding_step": "name",
            "onboarding_completed": False,
        }

    @staticmethod
    def _extract_email(message: str) -> Optional[str]:
        """Find and normalise a real email anywhere in the message."""
        for candidate in EMAIL_RE.findall(message):
            try:
                validated = validate_email(candidate, check_deliverability=False)
            except EmailNotValidError:
                continue
            normalised = getattr(validated, "normalized", None) or getattr(validated, "email", None)
            if normalised:
                return normalised.lower()
        return None

    # ------------------------------------------------------------- onboarding
    async def _handle_onboarding(
        self, db: Session, user_id: int, message: str, channel: str
    ) -> Dict[str, Any]:
        conversation = MessageRepository.get_or_create_conversation(db, user_id, channel)

        result = await onboarding_service.advance(db, user_id, message)

        self._persist(db, conversation.id, user_id, message, result["response"], channel)

        logger.info(
            "Onboarding turn | user=%s | captured=%s | next=%s | done=%s",
            user_id, result.get("captured"), result["onboarding_step"],
            result["onboarding_completed"],
        )

        return {
            "response": result["response"],
            "onboarding_step": result["onboarding_step"],
            "onboarding_completed": result["onboarding_completed"],
        }

    # ----------------------------------------------------------- regular chat
    async def _handle_regular_chat(
        self, db: Session, user_id: int, message: str, channel: str
    ) -> Dict[str, Any]:
        profile = UserRepository.get_or_create_profile(db, user_id)
        conversation = MessageRepository.get_or_create_conversation(db, user_id, channel)

        saved = MessageRepository.create(db, conversation.id, user_id, "user", message, channel)
        saved_id = getattr(saved, "id", None)

        # --- reminder ingestion, before the model writes anything ---------
        reminder_result = None
        try:
            reminder_result = await reminder_service.maybe_create(
                db, user_id, message, profile.timezone or "UTC"
            )
        except Exception:
            # Rollback is required or the session stays poisoned and the
            # assistant-message insert below fails too. This assumes
            # MessageRepository.create() commits its own row (yours does); if
            # it ever stops committing, the user turn above is lost here.
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
            # Do NOT store a failure string as an assistant message.
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
        if not reminder_result:
            return ""

        tz = profile.timezone or "UTC"

        if reminder_result["status"] == "created":
            content = reminder_result["reminder"].content
            return (
                "\n\nSYSTEM FACT — this already happened, treat it as done:\n"
                f'A reminder has been saved to the database: "{content}", scheduled for '
                f"{reminder_result['local_time']} ({tz}). It will be emailed to them "
                "automatically at that time. Confirm it warmly in one or two sentences, "
                "repeating the time back so they can check it. Do not say you are unable "
                "to set reminders and do not suggest a phone alarm."
            )

        if reminder_result["status"] == "needs_time":
            return (
                "\n\nSYSTEM FACT — this already happened, treat it as done:\n"
                f'They want a reminder for "{reminder_result["content"]}" but gave no '
                "usable time. Ask them what date and time they want it, in one short "
                "sentence. Do not say you are unable to set reminders."
            )

        return ""

    @staticmethod
    async def _remember(user_id: int, message: str, llm_response: str) -> None:
        try:
            vector = await embedding_service.embed(message)
            await memory_service.store_memory(user_id, f"User: {message}", vector)
            vector = await embedding_service.embed(llm_response)
            await memory_service.store_memory(user_id, f"Assistant: {llm_response}", vector)
        except Exception:
            logger.exception("Memory storage failed for user %s", user_id)

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _subject(step: Optional[str]) -> str:
        from app.services.onboarding_service import STEP_SUBJECT
        return STEP_SUBJECT.get(step or "", "the rest of their setup details")

    @staticmethod
    async def _say(note: str, profile=None, system: Optional[str] = None) -> str:
        """Every user-facing line in this file is generated here."""
        if system is None:
            language = (getattr(profile, "language", None) or "en") if profile else "en"
            system = (
                "You are a warm AI companion talking to someone in a chat window.\n"
                "Write one or two short sentences of plain conversational prose. No lists, "
                "no bullet points, no markdown, no restating of instructions.\n"
                "Ask at most one question.\n"
                f"Reply in the language with ISO 639-1 code '{language}'."
            )
        try:
            return await llm_service.chat(
                system=system,
                user=f"Say this to them now: {note}",
                temperature=0.6,
                max_tokens=250,
            )
        except LLMError:
            logger.exception("Groq unavailable while generating a chat-service reply")
            return LLM_DOWN

    @staticmethod
    def _persist(
        db: Session,
        conversation_id: int,
        user_id: int,
        user_message: str,
        assistant_message: str,
        channel: str,
    ) -> None:
        try:
            MessageRepository.create(db, conversation_id, user_id, "user", user_message, channel)
            MessageRepository.create(db, conversation_id, user_id, "assistant", assistant_message, channel)
        except Exception:
            db.rollback()
            logger.exception("Could not persist onboarding messages for user %s", user_id)


chat_service = ChatService()