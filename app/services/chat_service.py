"""
Chat Service - Using Actual UserRepository
✅ FIXED: Uses existing get_by_phone() for duplicate checking
✅ Saves to user.phone_number with proper normalization
"""
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
from app.schemas.schemas import PIIMasker

logger = logging.getLogger(__name__)

LLM_DOWN = "I'm having trouble thinking straight for a second — could you send that again?"


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

    async def process_message(
        self,
        db: Session,
        message: str,
        user_id: Optional[int] = None,
        channel: str = "web",
    ) -> Dict[str, Any]:
        """Main entry point."""
        message = (message or "").strip()
        if not message:
            return {"error": "Empty message"}

        if user_id is None:
            return {"error": "Authentication required"}

        user = UserRepository.get_by_id(db, user_id)
        if not user:
            return {"error": "User not found"}

        profile = UserRepository.get_or_create_profile(db, user_id)

        if not profile.onboarding_completed:
            return await self._handle_onboarding(db, user_id, message, channel, profile)
        
        return await self._handle_regular_chat(db, user_id, message, channel)

    async def _handle_onboarding(
        self, db: Session, user_id: int, message: str, channel: str, profile
    ) -> Dict[str, Any]:
        """Handle onboarding questions."""
        current_step = profile.onboarding_step or onboarding_service.STEP_TIER

        response = ""
        next_step = current_step
        completed = False

        # ============================================================================
        # STEP 0: TIER
        # ============================================================================
        if current_step == onboarding_service.STEP_TIER:
            parsed_value = await onboarding_service.parse_tier(message)

            if parsed_value is None:
                response = f"Sorry, I didn't understand that. {onboarding_service.get_step_prompt(current_step)}"
                conversation = MessageRepository.get_or_create_conversation(db, user_id, channel)
                self._persist(db, conversation.id, user_id, message, response, channel)

                return {
                    "response": response,
                    "onboarding_step": current_step,
                    "onboarding_completed": False,
                }

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
                conversation = MessageRepository.get_or_create_conversation(db, user_id, channel)
                self._persist(db, conversation.id, user_id, message, response, channel)

                return {
                    "response": response,
                    "onboarding_step": current_step,
                    "onboarding_completed": False,
                }

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
                response = f"Sorry, I didn't understand that. {onboarding_service.get_step_prompt(current_step)}\n\nTry: Asia/Karachi, or just tell me your city (lahore, karachi, dubai, etc.)"
                conversation = MessageRepository.get_or_create_conversation(db, user_id, channel)
                self._persist(db, conversation.id, user_id, message, response, channel)

                return {
                    "response": response,
                    "onboarding_step": current_step,
                    "onboarding_completed": False,
                }

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
                conversation = MessageRepository.get_or_create_conversation(db, user_id, channel)
                self._persist(db, conversation.id, user_id, message, response, channel)

                return {
                    "response": response,
                    "onboarding_step": current_step,
                    "onboarding_completed": False,
                }

            profile.language = parsed_value
            next_step = onboarding_service.get_next_step(current_step)
            confirmation = onboarding_service.get_confirmation_message(current_step, parsed_value)
            next_prompt = onboarding_service.get_step_prompt(next_step)
            response = f"{confirmation}\n\n{next_prompt}"

        # ============================================================================
        # STEP 4: WHATSAPP PHONE - SAVE TO phone_number COLUMN
        # ============================================================================
        elif current_step == onboarding_service.STEP_WHATSAPP:
            logger.info(f"WHATSAPP STEP: Processing message for user {user_id}")
            
            parsed_value = await onboarding_service.parse_whatsapp_phone(message, db=db)
            logger.info(f"WHATSAPP STEP: parse_whatsapp_phone returned: {parsed_value}")

            if parsed_value == "DUPLICATE":
                logger.warning(f"WHATSAPP STEP: Duplicate phone detected for user {user_id}")
                response = "This phone number is already linked to another account. Please try a different number, or reply 'skip' to continue without WhatsApp."
                conversation = MessageRepository.get_or_create_conversation(db, user_id, channel)
                self._persist(db, conversation.id, user_id, message, response, channel)

                return {
                    "response": response,
                    "onboarding_step": current_step,
                    "onboarding_completed": False,
                }

            elif parsed_value == "":
                logger.warning(f"WHATSAPP STEP: Invalid format for user {user_id}")
                response = "Invalid phone format. Please enter a valid phone number (e.g., +12345678901) or reply 'skip'."
                conversation = MessageRepository.get_or_create_conversation(db, user_id, channel)
                self._persist(db, conversation.id, user_id, message, response, channel)

                return {
                    "response": response,
                    "onboarding_step": current_step,
                    "onboarding_completed": False,
                }

            elif parsed_value is None:
                # User skipped
                logger.info(f"WHATSAPP STEP: User {user_id} skipped WhatsApp")
                next_step = onboarding_service.get_next_step(current_step)
                completed = onboarding_service.is_complete(next_step)
                profile.onboarding_completed = True
                response = "🎉 Welcome! Onboarding complete. You can now start chatting!"

            else:
                # ✅ CRITICAL: Valid phone - MUST SAVE TO DATABASE
                logger.info(f"WHATSAPP STEP: Valid phone parsed: {PIIMasker.mask_phone(parsed_value)}")
                
                try:
                    logger.info(f"WHATSAPP STEP: Fetching user {user_id} from database")
                    user = UserRepository.get_by_id(db, user_id)
                    
                    if not user:
                        logger.error(f"❌ WHATSAPP STEP: User not found for id {user_id}")
                        response = "Error saving WhatsApp. User not found. Please try again."
                        conversation = MessageRepository.get_or_create_conversation(db, user_id, channel)
                        self._persist(db, conversation.id, user_id, message, response, channel)
                        return {
                            "response": response,
                            "onboarding_step": current_step,
                            "onboarding_completed": False,
                        }
                    
                    logger.info(f"WHATSAPP STEP: User found: {user.id}, email: {user.email}")
                    
                    # ✅ SET phone_number (using UserRepository normalization)
                    normalized_phone = UserRepository._normalize_phone(parsed_value)
                    logger.info(f"WHATSAPP STEP: Setting user.phone_number = {PIIMasker.mask_phone(normalized_phone)}")
                    user.phone_number = normalized_phone
                    logger.info(f"WHATSAPP STEP: Attribute set, user.phone_number = {user.phone_number}")
                    
                    # ✅ FLUSH TO CATCH ERRORS
                    logger.info(f"WHATSAPP STEP: db.add(user) ...")
                    db.add(user)
                    logger.info(f"WHATSAPP STEP: db.flush() ...")
                    db.flush()
                    logger.info(f"WHATSAPP STEP: Flush successful, committing ...")
                    
                    # ✅ COMMIT
                    db.commit()
                    logger.info(f"✅ WHATSAPP STEP: db.commit() SUCCESS! Phone saved for user {user_id}")
                    
                    # Verify it was saved
                    db.refresh(user)
                    logger.info(f"✅ WHATSAPP STEP: After refresh, user.phone_number = {user.phone_number}")
                
                except Exception as e:
                    logger.error(f"❌ WHATSAPP STEP: Exception when saving: {type(e).__name__}: {str(e)}")
                    db.rollback()
                    response = f"Error saving WhatsApp: {str(e)}. Please try again."
                    conversation = MessageRepository.get_or_create_conversation(db, user_id, channel)
                    self._persist(db, conversation.id, user_id, message, response, channel)
                    return {
                        "response": response,
                        "onboarding_step": current_step,
                        "onboarding_completed": False,
                    }

                # After successful save
                next_step = onboarding_service.get_next_step(current_step)
                completed = onboarding_service.is_complete(next_step)
                profile.onboarding_completed = True
                confirmation = onboarding_service.get_confirmation_message(current_step, parsed_value)
                response = f"{confirmation}\n\n🎉 Welcome! Onboarding complete. You can now start chatting!"

        # ============================================================================
        # SAVE PROFILE AND PERSIST
        # ============================================================================
        try:
            profile.onboarding_step = next_step
            db.add(profile)
            db.commit()
            logger.info(f"Profile saved: step={next_step}, completed={completed}, user={user_id}")
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to save profile: {e}", extra={"user_id": user_id})
            return {
                "response": "Sorry, there was an error. Please try again.",
                "onboarding_step": current_step,
                "onboarding_completed": False,
            }

        conversation = MessageRepository.get_or_create_conversation(db, user_id, channel)
        self._persist(db, conversation.id, user_id, message, response, channel)

        return {
            "response": response,
            "onboarding_step": next_step,
            "onboarding_completed": completed,
        }

    async def _handle_regular_chat(
        self, db: Session, user_id: int, message: str, channel: str
    ) -> Dict[str, Any]:
        """Handle regular chat after onboarding is complete."""
        profile = UserRepository.get_or_create_profile(db, user_id)
        conversation = MessageRepository.get_or_create_conversation(db, user_id, channel)

        saved = MessageRepository.create(db, conversation.id, user_id, "user", message, channel)
        saved_id = getattr(saved, "id", None)

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
        """Save messages to conversation."""
        try:
            MessageRepository.create(db, conversation_id, user_id, "user", user_message, channel)
            MessageRepository.create(db, conversation_id, user_id, "assistant", assistant_message, channel)
        except Exception:
            db.rollback()
            logger.exception("Could not persist messages for user %s", user_id)


chat_service = ChatService()