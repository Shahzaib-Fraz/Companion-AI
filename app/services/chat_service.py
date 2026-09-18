
import logging
import re
from datetime import datetime, timedelta
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
from app.services.whatsapp_service import whatsapp_service
from app.core.config import settings
from app.schemas.schemas import PIIMasker

logger = logging.getLogger(__name__)

LLM_DOWN = "I'm having trouble thinking straight for a second — could you send that again?"

# P1-17: how long a WhatsApp verification code is valid, and how many
# wrong guesses are allowed before making the person re-enter their
# number from scratch (and get a fresh code).
PHONE_CODE_TTL = timedelta(minutes=10)
PHONE_CODE_MAX_ATTEMPTS = 5

# P1-20: cheap, local heuristic - deliberately wide, not precise. A
# false positive (an ordinary message that happens to match) just means
# the real, more accurate LLM-based check in reminder_service still
# runs, so nothing is lost by over-triggering. A false negative (a
# genuine reminder request phrased in a way this doesn't catch - e.g. a
# bare "yes do it" following up on an earlier reminder conversation,
# with no time/reminder word of its own) would incorrectly skip
# reminder creation for that message; that's a real, known limitation
# of a cheap gate, not something this regex can fully close without
# becoming an LLM call itself, which defeats the point.
_REMINDER_INTENT_PATTERN = re.compile(
    r"remind(er|s)?\b"
    r"|don'?t forget"
    r"|remember to\b"
    r"|wake me\b"
    r"|alert me\b"
    r"|notify me\b"
    r"|\btomorrow\b"
    r"|\btonight\b"
    r"|\bnext\s+(week|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
    r"|\bin\s+\d+\s*(minute|min|hour|hr|day|week)s?\b"
    r"|\bat\s+\d{1,2}(:\d{2})?\s*(am|pm)?\b"
    r"|\b\d{1,2}(:\d{2})?\s*(am|pm)\b",
    re.IGNORECASE,
)


class ChatService:
    """Chat service - saves WhatsApp to existing phone_number column."""

    @staticmethod
    def _extract_durable_facts(user_message: str) -> Dict[str, Any]:
        """Extract durable facts from user message."""
        if not user_message or not user_message.strip():
            return {"facts": []}

        facts = []
        msg_lower = user_message.lower()

        if "my name is" in msg_lower or "call me" in msg_lower:
            name = ChatService._extract_name(user_message)
            if name:
                facts.append({
                    "fact": f"User's name is {name}",
                    "category": "name",
                    "confidence": 0.95,
                })

        if "work from" in msg_lower or "live in" in msg_lower or "based in" in msg_lower:
            location = ChatService._extract_location(user_message)
            if location:
                facts.append({
                    "fact": f"User works/lives in {location}",
                    "category": "location",
                    "confidence": 0.85,
                })

        if "prefer" in msg_lower or "like" in msg_lower or "enjoy" in msg_lower:
            prefs = ChatService._extract_preferences(user_message)
            facts.extend(prefs)

        return {"facts": facts}

    @staticmethod
    def _extract_name(text: str) -> Optional[str]:
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
        for kw in ["work from", "live in", "based in"]:
            if kw in text.lower():
                parts = text.lower().split(kw)
                if len(parts) > 1:
                    location = " ".join(parts[1].strip().split()[0:2])
                    return location
        return None

    @staticmethod
    def _extract_preferences(text: str) -> list:
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

    @staticmethod
    def _looks_like_it_might_involve_a_reminder(message: str) -> bool:
        """P1-20: see the module-level docstring and _REMINDER_INTENT_PATTERN's comment for what this does and doesn't guarantee."""
        return bool(_REMINDER_INTENT_PATTERN.search(message or ""))

    async def process_message(
        self,
        db: Session,
        message: str,
        user_id: Optional[int] = None,
        channel: str = "web",
    ) -> Dict[str, Any]:
        """
        Main entry point.

        P1-19 FIX: every error path carries a stable "error_code"
        alongside the human-readable "error" text, so route handlers can
        map it to a real HTTP status.
        """
        message = (message or "").strip()
        if not message:
            return {"error_code": "INVALID_MESSAGE", "error": "Empty message"}

        if user_id is None:
            return {"error_code": "UNAUTHENTICATED", "error": "Authentication required"}

        user = UserRepository.get_by_id(db, user_id)
        if not user:
            return {"error_code": "USER_NOT_FOUND", "error": "User not found"}

        profile = UserRepository.get_or_create_profile(db, user_id)

        if not profile.onboarding_completed:
            return await self._handle_onboarding(db, user_id, message, channel, profile)

        return await self._handle_regular_chat(db, user_id, message, channel)

    async def _handle_onboarding(
        self, db: Session, user_id: int, message: str, channel: str, profile
    ) -> Dict[str, Any]:
        """
        Handle onboarding questions.

        P1-14 FIX: every branch below now just sets `response` (and
        profile state, where relevant) and falls through to ONE shared
        tail at the end, instead of persisting and committing (or, in
        several branches, persisting with NO commit at all)
        independently. An external call that happens mid-branch (the
        WhatsApp send in the phone-verification flow) still runs BEFORE
        any profile mutation in the affected branches, so a failed send
        never leaves a half-written state to be committed regardless.
        """
        current_step = profile.onboarding_step or onboarding_service.STEP_TIER

        response = ""
        next_step = current_step
        completed = False

        try:
            # ============================================================================
            # STEP 0: TIER
            # ============================================================================
            if current_step == onboarding_service.STEP_TIER:
                parsed_value = await onboarding_service.parse_tier(message)

                if parsed_value is None:
                    response = f"Sorry, I didn't understand that. {onboarding_service.get_step_prompt(current_step)}"
                else:
                    profile.account_tier = parsed_value
                    next_step = onboarding_service.get_next_step(current_step)
                    confirmation = onboarding_service.get_confirmation_message(current_step, parsed_value)
                    next_prompt = onboarding_service.get_step_prompt(next_step)
                    response = f"{confirmation}\n\n{next_prompt}"

            # ============================================================================
            # STEP 1: NAME
            # ============================================================================
            elif current_step == onboarding_service.STEP_NAME:
                parsed_value = onboarding_service.parse_name(message)

                if parsed_value is None:
                    response = f"Sorry, I didn't understand that. {onboarding_service.get_step_prompt(current_step)}"
                else:
                    profile.display_name = parsed_value
                    next_step = onboarding_service.get_next_step(current_step)
                    confirmation = onboarding_service.get_confirmation_message(current_step, parsed_value)
                    next_prompt = onboarding_service.get_step_prompt(next_step)
                    response = f"{confirmation}\n\n{next_prompt}"

            # ============================================================================
            # STEP 2: TIMEZONE
            # ============================================================================
            elif current_step == onboarding_service.STEP_TIMEZONE:
                parsed_value = await onboarding_service.parse_timezone(message)

                if parsed_value is None:
                    response = (
                        f"Sorry, I didn't understand that. {onboarding_service.get_step_prompt(current_step)}"
                        "\n\nTry: Asia/Karachi, or just tell me your city (lahore, karachi, dubai, etc.)"
                    )
                else:
                    profile.timezone = parsed_value
                    next_step = onboarding_service.get_next_step(current_step)
                    confirmation = onboarding_service.get_confirmation_message(current_step, parsed_value)
                    next_prompt = onboarding_service.get_step_prompt(next_step)
                    response = f"{confirmation}\n\n{next_prompt}"

            # ============================================================================
            # STEP 3: LANGUAGE
            # ============================================================================
            elif current_step == onboarding_service.STEP_LANGUAGE:
                parsed_value = onboarding_service.parse_language(message)

                if parsed_value is None:
                    response = f"Sorry, I didn't understand that. {onboarding_service.get_step_prompt(current_step)}"
                else:
                    profile.language = parsed_value
                    next_step = onboarding_service.get_next_step(current_step)
                    confirmation = onboarding_service.get_confirmation_message(current_step, parsed_value)
                    next_prompt = onboarding_service.get_step_prompt(next_step)
                    response = f"{confirmation}\n\n{next_prompt}"

            # ============================================================================
            # STEP 4: WHATSAPP PHONE - two-phase ownership verification (P1-17)
            # ============================================================================
            #
            #   Phase A (profile.pending_phone is empty): the message is a
            #     phone number. On a valid, non-duplicate one, generate a
            #     6-digit code, WhatsApp-send it via a template message, and
            #     stash it on the profile with a 10-minute expiry.
            #   Phase B (profile.pending_phone is set): the message is
            #     (hopefully) that code. Only on a match does the number get
            #     copied to user.phone_number - the thing notifications
            #     actually go to.
            #
            # Requires a "phone_verification_code" template approved in Meta
            # Business Manager.
            elif current_step == onboarding_service.STEP_WHATSAPP:
                if profile.pending_phone:
                    # ---------------- Phase B: verifying the code ----------------
                    entered = message.strip()

                    if entered.lower() in ("skip", "later"):
                        profile.pending_phone = None
                        profile.pending_phone_code = None
                        profile.pending_phone_code_expires_at = None
                        profile.pending_phone_attempts = 0
                        next_step = onboarding_service.get_next_step(current_step)
                        completed = onboarding_service.is_complete(next_step)
                        profile.onboarding_completed = True
                        response = "No problem, skipped WhatsApp.\n\n🎉 Welcome! Onboarding complete. You can now start chatting!"

                    elif entered.lower() in ("resend", "new code", "resend code"):
                        code = onboarding_service.generate_verification_code()
                        profile.pending_phone_code = code
                        profile.pending_phone_code_expires_at = datetime.utcnow() + PHONE_CODE_TTL
                        profile.pending_phone_attempts = 0
                        sent = await whatsapp_service.send_template_message(
                            phone_number=profile.pending_phone,
                            template_name="phone_verification_code",
                            parameters=[code],
                        )
                        response = (
                            f"Sent a new code to {profile.pending_phone}. What's the code?"
                            if sent else
                            "Couldn't send a new code right now — reply 'skip' to continue without "
                            "WhatsApp, or try again in a moment."
                        )

                    elif (
                        not profile.pending_phone_code_expires_at
                        or datetime.utcnow() > profile.pending_phone_code_expires_at
                    ):
                        # Expired - auto-resend rather than making them ask.
                        code = onboarding_service.generate_verification_code()
                        profile.pending_phone_code = code
                        profile.pending_phone_code_expires_at = datetime.utcnow() + PHONE_CODE_TTL
                        profile.pending_phone_attempts = 0
                        await whatsapp_service.send_template_message(
                            phone_number=profile.pending_phone,
                            template_name="phone_verification_code",
                            parameters=[code],
                        )
                        response = "That code expired — I've sent you a new one. What's the code?"

                    elif entered == profile.pending_phone_code:
                        # Verified - NOW it's safe to actually save it.
                        user_obj = UserRepository.get_by_id(db, user_id)
                        user_obj.phone_number = profile.pending_phone
                        db.add(user_obj)

                        profile.pending_phone = None
                        profile.pending_phone_code = None
                        profile.pending_phone_code_expires_at = None
                        profile.pending_phone_attempts = 0

                        next_step = onboarding_service.get_next_step(current_step)
                        completed = onboarding_service.is_complete(next_step)
                        profile.onboarding_completed = True
                        response = "✅ WhatsApp verified and saved!\n\n🎉 Welcome! Onboarding complete. You can now start chatting!"

                    else:
                        profile.pending_phone_attempts = (profile.pending_phone_attempts or 0) + 1
                        if profile.pending_phone_attempts >= PHONE_CODE_MAX_ATTEMPTS:
                            profile.pending_phone = None
                            profile.pending_phone_code = None
                            profile.pending_phone_code_expires_at = None
                            profile.pending_phone_attempts = 0
                            response = (
                                "Too many incorrect attempts. Let's try again — "
                                f"{onboarding_service.get_step_prompt(current_step)}"
                            )
                        else:
                            remaining = PHONE_CODE_MAX_ATTEMPTS - profile.pending_phone_attempts
                            response = (
                                f"That code doesn't match. Try again, or reply 'resend' for a new one. "
                                f"({remaining} attempt{'s' if remaining != 1 else ''} left)"
                            )

                else:
                    # ---------------- Phase A: collecting the phone number ----------------
                    parsed_value = await onboarding_service.parse_whatsapp_phone(message, db=db)

                    if parsed_value == "DUPLICATE":
                        response = (
                            "This phone number is already linked to another account. "
                            "Please try a different number, or reply 'skip' to continue without WhatsApp."
                        )

                    elif parsed_value == "":
                        response = "Invalid phone format. Please enter a valid phone number (e.g., +12345678901) or reply 'skip'."

                    elif parsed_value is None:
                        # Skipped before ever entering a number.
                        next_step = onboarding_service.get_next_step(current_step)
                        completed = onboarding_service.is_complete(next_step)
                        profile.onboarding_completed = True
                        response = "🎉 Welcome! Onboarding complete. You can now start chatting!"

                    else:
                        normalized_phone = UserRepository._normalize_phone(parsed_value)
                        code = onboarding_service.generate_verification_code()

                        sent = await whatsapp_service.send_template_message(
                            phone_number=normalized_phone,
                            template_name="phone_verification_code",
                            parameters=[code],
                        )

                        if not sent:
                            response = (
                                "Couldn't send a verification code to that number. Please double-check "
                                "it, or reply 'skip' to continue without WhatsApp."
                            )
                        else:
                            profile.pending_phone = normalized_phone
                            profile.pending_phone_code = code
                            profile.pending_phone_code_expires_at = datetime.utcnow() + PHONE_CODE_TTL
                            profile.pending_phone_attempts = 0
                            response = (
                                f"I texted a 6-digit code to {normalized_phone} on WhatsApp. "
                                "What's the code? (or reply 'skip')"
                            )

            # ============================================================================
            # ONE SHARED TAIL (P1-14): save profile + persist both messages + commit ONCE
            # ============================================================================
            profile.onboarding_step = next_step
            db.add(profile)

            conversation = MessageRepository.get_or_create_conversation(db, user_id, channel)
            MessageRepository.create(db, conversation.id, user_id, "user", message, channel)
            MessageRepository.create(db, conversation.id, user_id, "assistant", response, channel)

            db.commit()
            logger.info(f"Onboarding turn saved: step={next_step}, completed={completed}, user={user_id}")

        except Exception as e:
            db.rollback()
            logger.error(f"Onboarding turn failed for user {user_id}: {e}", exc_info=True)
            return {
                "response": "Sorry, there was an error. Please try again.",
                "onboarding_step": current_step,
                "onboarding_completed": False,
            }

        return {
            "response": response,
            "onboarding_step": next_step,
            "onboarding_completed": completed,
        }

    async def _handle_regular_chat(
        self, db: Session, user_id: int, message: str, channel: str
    ) -> Dict[str, Any]:
        """
        Handle regular chat after onboarding is complete.

        P1-14 FIX: this used to have no commit point at all on the
        success path. Now commits explicitly at two points: once after
        the user's message and any reminder are persisted (a real,
        complete unit of work independent of the LLM call that
        follows), and once after the assistant's reply. An LLM failure
        between those two commits can no longer risk losing data that
        had already genuinely happened.

        P1-20 FIX: reminder_service.maybe_create() is now only called
        when _looks_like_it_might_involve_a_reminder(message) is true -
        see that method and _REMINDER_INTENT_PATTERN for what this does
        and doesn't guarantee.
        """
        profile = UserRepository.get_or_create_profile(db, user_id)
        conversation = MessageRepository.get_or_create_conversation(db, user_id, channel)

        saved = MessageRepository.create(db, conversation.id, user_id, "user", message, channel)
        saved_id = getattr(saved, "id", None)

        reminder_result = None
        if self._looks_like_it_might_involve_a_reminder(message):
            try:
                reminder_result = await reminder_service.maybe_create(
                    db, user_id, message, profile.timezone or "UTC"
                )
            except Exception:
                logger.exception("Reminder ingestion failed for user %s", user_id)
                reminder_result = None

        try:
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Failed to persist chat turn for user %s", user_id)
            return {
                "response": LLM_DOWN,
                "error_code": "PERSISTENCE_FAILED",
                "error": "could_not_save_message",
                "reminder_set": False,
            }

        system, messages = await context_builder.build(
            db, user_id, message, exclude_message_id=saved_id
        )
        system += self._reminder_fact(reminder_result, profile)

        try:
            llm_response = await llm_service.chat(
                system=system,
                messages=messages,
                usage_context={"db": db, "user_id": user_id, "purpose": "chat"},
            )
        except LLMError:
            logger.exception("Groq failed on a chat turn for user %s", user_id)
            return {
                "response": LLM_DOWN,
                "error_code": "LLM_UNAVAILABLE",
                "error": "llm_unavailable",
                "reminder_set": bool(reminder_result and reminder_result.get("status") == "created"),
            }

        MessageRepository.create(db, conversation.id, user_id, "assistant", llm_response, channel)

        if (profile.account_tier or "") == "premium":
            await self._remember(user_id, message, llm_response)

        try:
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Failed to persist assistant reply for user %s", user_id)
            # The LLM response was already generated - still return it
            # rather than pretending the whole turn failed, even though
            # it couldn't be saved this time.

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

        if reminder_result["status"] == "needs_clarification":
            return (
                "\n\nSYSTEM FACT — this already happened, treat it as done:\n"
                f'They asked for a reminder but only referred to it as '
                f'"{reminder_result["content"]}", and there\'s nothing in the recent '
                "conversation to tell what that is. Ask them, in one sentence, what "
                "exactly they want to be reminded about."
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
                return

            threshold = settings.MEMORY_EXTRACTION_CONFIDENCE_THRESHOLD
            filtered_facts = [f for f in facts if f["confidence"] >= threshold]

            if not filtered_facts:
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

    @staticmethod
    def _persist(
        db: Session,
        conversation_id: int,
        user_id: int,
        user_message: str,
        assistant_message: str,
        channel: str,
    ) -> None:
        """
        Save messages to conversation.

        No longer called from _handle_onboarding (P1-14 inlined message
        persistence into that function's single shared commit tail
        instead, so a commit failure and a persistence failure share one
        rollback path). Left defined, unused internally, in case
        anything outside this file still calls it directly.
        """
        try:
            MessageRepository.create(db, conversation_id, user_id, "user", user_message, channel)
            MessageRepository.create(db, conversation_id, user_id, "assistant", assistant_message, channel)
        except Exception:
            db.rollback()
            logger.exception("Could not persist messages for user %s", user_id)


chat_service = ChatService()