"""
OnboardingService - Using Actual UserRepository Methods
✅ FIXED: Uses get_by_phone() which handles normalization
"""
from typing import Optional
import logging

logger = logging.getLogger(__name__)

class OnboardingService:
    """Onboarding service with all parsing methods fixed."""
    
    # Constants
    STEP_TIER = 0
    STEP_NAME = 1
    STEP_TIMEZONE = 2
    STEP_LANGUAGE = 3
    STEP_WHATSAPP = 4
    STEP_COMPLETE = 5
    
    STEP_NAMES = {
        STEP_TIER: "tier",
        STEP_NAME: "name",
        STEP_TIMEZONE: "timezone",
        STEP_LANGUAGE: "language",
        STEP_WHATSAPP: "whatsapp",
        STEP_COMPLETE: "complete",
    }
    
    # ============================================================================
    # PARSE METHODS - ALL RETURN None WHEN NOT UNDERSTOOD (NO DEFAULTS!)
    # ============================================================================
    
    async def parse_tier(self, message: str) -> Optional[str]:
        """
        Parse tier from user message.
        ✅ FIXED: Returns None if not understood (no auto "free")
        """
        msg = message.lower().strip()
        
        # Only accept if user explicitly says free/premium
        if "free" in msg or "basic" in msg or "standard" in msg:
            return "free"
        
        if "premium" in msg or "pro" in msg or "plus" in msg:
            return "premium"
        
        # ✅ CRITICAL FIX: Return None = "I didn't understand"
        return None
    
    def parse_name(self, message: str) -> Optional[str]:
        """
        Parse name from user message.
        ✅ FIXED: Returns None if not valid (no default "User")
        """
        msg = message.strip()
        
        if not msg or len(msg) < 2:
            return None
        
        # Check if mostly alphabetic
        alpha_count = sum(1 for c in msg if c.isalpha())
        if alpha_count / len(msg) < 0.7:
            return None
        
        # ✅ Return actual name or None (never default)
        return msg.strip()
    
    async def parse_timezone(self, message: str) -> Optional[str]:
        """
        Parse timezone from user message.
        ✅ FIXED: Returns None if not understood (no default UTC)
        """
        msg = message.lower().strip()
        
        # Timezone format: "Asia/Karachi"
        if "/" in msg:
            return msg
        
        # City mapping
        CITY_TIMEZONE_MAP = {
            "karachi": "Asia/Karachi",
            "lahore": "Asia/Karachi",
            "islamabad": "Asia/Karachi",
            "peshawar": "Asia/Karachi",
            "quetta": "Asia/Karachi",
            "multan": "Asia/Karachi",
            "faisalabad": "Asia/Karachi",
            "rawalpindi": "Asia/Karachi",
            "dubai": "Asia/Dubai",
            "london": "Europe/London",
            "new york": "America/New_York",
            "tokyo": "Asia/Tokyo",
            "singapore": "Asia/Singapore",
            "bangkok": "Asia/Bangkok",
            "mumbai": "Asia/Kolkata",
            "delhi": "Asia/Kolkata",
            "pakistan": "Asia/Karachi",
            "uk": "Europe/London",
            "usa": "America/New_York",
        }
        
        for city, tz in CITY_TIMEZONE_MAP.items():
            if city in msg:
                return tz
        
        # ✅ Return None if no match (never default to UTC)
        return None
    
    def parse_language(self, message: str) -> Optional[str]:
        """
        Parse language from user message.
        ✅ FIXED: Returns None if not understood (no default "en")
        """
        msg = message.lower().strip()
        
        LANGUAGE_MAP = {
            "english": "en", "en": "en",
            "urdu": "ur", "ur": "ur", "اردو": "ur",
            "spanish": "es", "es": "es",
            "french": "fr", "fr": "fr",
            "german": "de", "de": "de",
            "chinese": "zh", "zh": "zh",
            "arabic": "ar", "ar": "ar",
            "hindi": "hi", "hi": "hi",
        }
        
        for lang_name, code in LANGUAGE_MAP.items():
            if lang_name in msg:
                return code
        
        # ✅ Return None if no match (never default to "en")
        return None
    
    async def parse_whatsapp_phone(self, message: str, db=None) -> Optional[str]:
        """
        Parse WhatsApp phone from user message.
        ✅ Uses existing UserRepository.get_by_phone() which handles normalization
        
        Returns:
        - phone string if valid and unique
        - "DUPLICATE" if phone already exists
        - "" if invalid format
        - None if user typed "skip"
        """
        msg = message.lower().strip()
        
        # Handle skip
        if msg == "skip" or msg == "later":
            return None
        
        # Extract digits and +
        phone = "".join(c for c in message if c.isdigit() or c == "+")
        
        if not phone:
            return ""  # Invalid - no digits
        
        if len(phone) < 10 or len(phone) > 20:
            return ""  # Invalid length
        
        # ✅ Check for duplicates using existing UserRepository method
        # This handles phone normalization automatically
        if db:
            from app.repositories.user_repository import UserRepository
            existing = UserRepository.get_by_phone(db, phone)
            if existing:
                return "DUPLICATE"
        
        # ✅ Return normalized phone (UserRepository will normalize on save)
        return phone
    
    # ============================================================================
    # NAVIGATION
    # ============================================================================
    
    def get_next_step(self, current_step: int) -> int:
        """Get next step."""
        return min(current_step + 1, self.STEP_COMPLETE)
    
    def is_complete(self, step: int) -> bool:
        """Check if complete."""
        return step >= self.STEP_COMPLETE
    
    # ============================================================================
    # PROMPTS
    # ============================================================================
    
    def get_step_prompt(self, step: int) -> str:
        """Get prompt for step."""
        prompts = {
            self.STEP_TIER: "Which tier do you prefer: free or premium?",
            self.STEP_NAME: "What's your name?",
            self.STEP_TIMEZONE: "What's your timezone? (e.g., Asia/Karachi or your city)",
            self.STEP_LANGUAGE: "What language do you prefer? (en, ur, es, etc.)",
            self.STEP_WHATSAPP: "What's your WhatsApp phone? (or type 'skip')",
        }
        return prompts.get(step, "")
    
    def get_confirmation_message(self, step: int, value: str) -> str:
        """Get confirmation after parsing."""
        confirmations = {
            self.STEP_TIER: f"✅ Got it! You selected {value} tier.",
            self.STEP_NAME: f"✅ Nice to meet you, {value}!",
            self.STEP_TIMEZONE: f"✅ Your timezone is set to {value}.",
            self.STEP_LANGUAGE: f"✅ Language set to {value}.",
            self.STEP_WHATSAPP: f"✅ WhatsApp phone saved!",
        }
        return confirmations.get(step, "")


onboarding_service = OnboardingService()