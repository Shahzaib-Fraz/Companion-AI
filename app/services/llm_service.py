
import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

from groq import Groq

from app.core.config import settings

logger = logging.getLogger(__name__)

# qwen3: "none" disables thinking outright. gpt-oss rejects "none" (low/medium/
# high only) and is handled by the negotiation in _create(). Override in .env
# with GROQ_REASONING_EFFORT if you move to a model that needs a real value.
REASONING_EFFORT: str = getattr(settings, "GROQ_REASONING_EFFORT", None) or "none"

MAX_TOKEN_CEILING = 4096
MAX_ATTEMPTS = 6

# Reasoning that leaked into content despite our settings.
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# An UNCLOSED <think> means the response was cut off mid-thought: no answer
# was ever emitted, so the whole thing is discarded and retried with more room.
_THINK_OPEN_RE = re.compile(r"<think>", re.IGNORECASE)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


class LLMError(RuntimeError):
    """Groq could not produce a usable response. Never swallow this silently."""


class LLMService:
    def __init__(self):
        self.client = Groq(api_key=settings.GROQ_API_KEY)
        self.model = settings.GROQ_MODEL
        # Capability flags, negotiated once per process against the real model.
        self._json_mode_supported = True
        self._reasoning_effort_supported = True
        self._reasoning_format_supported = True

    # --------------------------------------------------------------- transport
    def _payload(self, messages, temperature, budget, json_mode) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": budget,
        }
        if json_mode and self._json_mode_supported:
            payload["response_format"] = {"type": "json_object"}

        if self._reasoning_effort_supported:
            payload["reasoning_effort"] = REASONING_EFFORT
        elif self._reasoning_format_supported:
            payload["reasoning_format"] = "hidden"

        return payload

    async def _create(
        self,
        *,
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: int,
        json_mode: bool = False,
    ) -> str:
        budget = max_tokens
        last_error: Optional[BaseException] = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            payload = self._payload(messages, temperature, budget, json_mode)

            try:
                response = await asyncio.to_thread(
                    self.client.chat.completions.create, **payload
                )
            except Exception as exc:
                last_error = exc
                detail = str(exc).lower()

                # Truncated before it could finish. More room, not fewer features.
                if ("max completion tokens" in detail or "json_validate_failed" in detail) \
                        and budget < MAX_TOKEN_CEILING:
                    budget = min(budget * 3, MAX_TOKEN_CEILING)
                    logger.warning(
                        "Groq ran out of output room; retrying with max_tokens=%d", budget
                    )
                    continue

                # Parameter this model does not accept: drop it and remember.
                if self._reasoning_effort_supported and "reasoning_effort" in detail:
                    logger.warning(
                        "Model %s rejected reasoning_effort=%r (%s); trying "
                        "reasoning_format=hidden instead",
                        self.model, REASONING_EFFORT, exc,
                    )
                    self._reasoning_effort_supported = False
                    continue

                if self._reasoning_format_supported and "reasoning_format" in detail:
                    logger.warning("Model %s rejected reasoning_format (%s)", self.model, exc)
                    self._reasoning_format_supported = False
                    continue

                if json_mode and self._json_mode_supported and (
                    "response_format" in detail or "json" in detail
                ):
                    logger.warning(
                        "Groq rejected JSON mode (%s); using prompt-only JSON from now on", exc
                    )
                    self._json_mode_supported = False
                    continue

                logger.error("Groq request failed: %s", exc)
                break

            text = self._content(response)
            if text:
                return text

            # HTTP 200 with nothing usable: almost always reasoning eating the
            # whole budget. Escalate rather than surfacing a dead reply.
            last_error = LLMError("Groq returned an empty message")
            if budget < MAX_TOKEN_CEILING:
                budget = min(budget * 3, MAX_TOKEN_CEILING)
                logger.warning(
                    "Empty content from %s on attempt %d; retrying with max_tokens=%d",
                    self.model, attempt, budget,
                )
                continue
            break

        raise LLMError(f"Groq call failed: {last_error}")

    @staticmethod
    def _content(response) -> str:
        """Pull usable text out of the response, discarding leaked reasoning."""
        try:
            message = response.choices[0].message
        except (AttributeError, IndexError):
            return ""

        text = (getattr(message, "content", None) or "").strip()
        if not text:
            return ""

        text = _THINK_BLOCK_RE.sub("", text).strip()

        # Unclosed <think>: cut off before any answer existed. Treat as empty so
        # the caller escalates the budget instead of showing a thought fragment.
        if _THINK_OPEN_RE.search(text):
            logger.warning("Discarding truncated reasoning output (no answer emitted)")
            return ""

        return text

    # ------------------------------------------------------------------- chat
    async def chat(
        self,
        *,
        system: str,
        messages: Optional[List[Dict[str, str]]] = None,
        user: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1400,
    ) -> str:
        """
        Natural-language generation. `messages` is a real multi-turn history
        ([{role, content}, ...]); `user` is appended as the final user turn.
        Raises LLMError.
        """
        payload: List[Dict[str, str]] = [{"role": "system", "content": system}]
        if messages:
            payload.extend(messages)
        if user is not None:
            payload.append({"role": "user", "content": user})

        if len(payload) == 1:
            raise LLMError("chat() called with no user content")

        return await self._create(
            messages=payload,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    # ---------------------------------------------------------------- extract
    async def extract(
        self,
        *,
        instruction: str,
        user_message: str,
        attempts: int = 2,
        max_tokens: int = 800,
    ) -> Optional[Dict[str, Any]]:
        """
        Structured extraction. Returns a dict, or None if Groq is unreachable
        or refuses to produce JSON after `attempts` tries.

        Deliberately has NO persona and NO language instruction. Its output is
        never shown to the user, so it always works in English.
        """
        system = (
            "You are a JSON extraction engine embedded in a backend service. "
            "You never chat, never greet, never apologise, never explain, and you "
            "do not reason out loud. You output exactly one JSON object and nothing else."
        )
        prompt = f"{instruction}\n\nMESSAGE:\n{user_message.strip()}"

        for attempt in range(1, attempts + 1):
            try:
                raw = await self._create(
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.0,
                    max_tokens=max_tokens,
                    json_mode=True,
                )
            except LLMError as exc:
                logger.warning("Extraction attempt %d/%d failed: %s", attempt, attempts, exc)
                continue

            data = self._loads(raw)
            if isinstance(data, dict):
                return data
            logger.warning(
                "Extraction attempt %d/%d produced unparseable output: %r",
                attempt, attempts, raw[:300],
            )

        return None

    @staticmethod
    def _loads(raw: str) -> Optional[Dict[str, Any]]:
        cleaned = _FENCE_RE.sub("", (raw or "").strip()).strip()
        match = _JSON_RE.search(cleaned)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    # ----------------------------------------------------------------- legacy
    async def get_response(
        self,
        context: str,
        user_tier: str = "free",
        language: str = "en",
    ) -> str:
        """
        Kept so older callers (routes, WhatsApp webhook) keep working.

        NOTE: this RAISES LLMError instead of returning "Error: ...". Wrap any
        route that calls it directly, or move it to chat().
        """
        system = (
            "You are a warm, thoughtful AI companion.\n"
            "Write in plain conversational prose. No markdown, no tables, no bullet lists. "
            "Separate ideas with blank lines.\n"
            "Match your length to the request: a short question gets one or two sentences; "
            "a request for a plan or explanation gets the full answer.\n"
            f"Reply in the language with ISO code '{language}'.\n"
            f"Account tier: {user_tier}."
        )
        return await self.chat(system=system, user=context)


llm_service = LLMService()