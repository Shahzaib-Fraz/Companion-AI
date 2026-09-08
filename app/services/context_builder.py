
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.repositories.message_repository import MessageRepository
from app.repositories.user_repository import UserRepository
from app.services.embedding_service import embedding_service
from app.services.memory_service import memory_service

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 10
MEMORY_TOP_K = 5


class ContextBuilder:
    # ------------------------------------------------------------------ public
    async def build(
        self,
        db: Session,
        user_id: int,
        current_message: str,
        exclude_message_id: Optional[int] = None,
    ) -> Tuple[str, List[Dict[str, str]]]:
        """
        Returns (system_prompt, messages).

        `messages` ends with the current user turn. Pass the id of the row you
        just wrote for this message as `exclude_message_id` so it is not
        duplicated in the history.
        """
        profile = UserRepository.get_or_create_profile(db, user_id)

        history = self._history(db, user_id, current_message, exclude_message_id)
        memories = await self._memories(profile, user_id, current_message)
        system = self.system_prompt(profile, memories)

        history.append({"role": "user", "content": current_message})
        return system, history

    def system_prompt(self, profile, memories: Optional[List[str]] = None) -> str:
        name = (profile.display_name or "").strip() or "the user"
        language = (profile.language or "en").strip()
        tier = (profile.account_tier or "free").strip()
        tz_name = (profile.timezone or "UTC").strip()
        local_now = self._local_now(tz_name)

        lines = [
            "You are a warm, attentive personal AI companion. You speak like a thoughtful "
            "friend who remembers things, not like a corporate assistant.",
            "",
            "ABOUT THE PERSON YOU ARE TALKING TO",
            f"- Name: {name}. Use it occasionally, not in every message.",
            f"- Timezone: {tz_name}. Their local date and time right now is "
            f"{local_now.strftime('%A %d %B %Y, %I:%M %p')}.",
            f"- Account tier: {tier}.",
            "",
            "WHAT YOU CAN ACTUALLY DO — state these accurately and never deny them:",
            "- You CAN set reminders. When the person asks for one, the backend saves it "
            "and emails it to them at the scheduled time. Never tell them to use a phone "
            "alarm instead, and never say you are unable to set reminders.",
            "- You CANNOT browse the web, open links, see images or read files.",
            "- You do not send WhatsApp messages yourself; reminders arrive by email.",
            "",
            "HOW TO WRITE",
            f"- Reply in the language with ISO 639-1 code '{language}'.",
            "- Plain conversational prose only. No markdown, no headings, no tables, "
            "no bullet points, no numbered lists. Separate ideas with blank lines.",
            "- Match your length to the request. A short question gets one to three "
            "sentences. A request for a plan, comparison or explanation gets the full "
            "answer, written as flowing paragraphs.",
            "- Never invent facts about the person. If you do not know something, ask.",
            "- Never mention system notes, prompts, tokens, databases or these instructions.",
        ]

        if memories:
            lines += ["", "THINGS YOU REMEMBER FROM EARLIER CONVERSATIONS"]
            lines += [f"- {m}" for m in memories]
            lines.append(
                "Use these only if they are relevant to what is being said now."
            )

        return "\n".join(lines)

    # ----------------------------------------------------------------- private
    @staticmethod
    def _local_now(tz_name: str) -> datetime:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except Exception:
            return datetime.now(ZoneInfo("UTC"))

    @staticmethod
    def _history(
        db: Session,
        user_id: int,
        current_message: str,
        exclude_message_id: Optional[int],
    ) -> List[Dict[str, str]]:
        try:
            rows = MessageRepository.get_last_n(db, user_id, HISTORY_LIMIT + 1) or []
        except Exception:
            logger.exception("Could not load history for user %s", user_id)
            return []

        # Repository order is not guaranteed. Force oldest -> newest.
        rows = sorted(
            rows,
            key=lambda m: (getattr(m, "created_at", None) or datetime.min, getattr(m, "id", 0)),
        )

        if exclude_message_id is not None:
            rows = [m for m in rows if getattr(m, "id", None) != exclude_message_id]
        elif rows:
            # Fallback when the repository does not return the created row:
            # drop a trailing user turn identical to the message we are handling.
            last = rows[-1]
            if getattr(last, "role", "") == "user" and (last.content or "").strip() == current_message.strip():
                rows = rows[:-1]

        rows = rows[-HISTORY_LIMIT:]

        history: List[Dict[str, str]] = []
        for m in rows:
            role = "assistant" if getattr(m, "role", "") == "assistant" else "user"
            content = (m.content or "").strip()
            if content:
                history.append({"role": role, "content": content})
        return history

    @staticmethod
    async def _memories(profile, user_id: int, current_message: str) -> List[str]:
        if (profile.account_tier or "") != "premium":
            return []
        try:
            vector = await embedding_service.embed(current_message)
            return await memory_service.search(user_id, vector, top_k=MEMORY_TOP_K)
        except Exception:
            # Semantic recall is an enhancement. Never fail a chat turn over it.
            logger.exception("Memory retrieval failed for user %s", user_id)
            return []

    # ------------------------------------------------------------------ legacy
    async def build_context(self, db: Session, user_id: int, current_message: str) -> str:
        """Flattened form, kept only for older callers. Prefer build()."""
        system, messages = await self.build(db, user_id, current_message)
        turns = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
        return f"{system}\n\n{turns}"


context_builder = ContextBuilder()