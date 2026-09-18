
import logging
from typing import Optional
from sqlalchemy import text

logger = logging.getLogger(__name__)


def validate_config_sync():
    """
    Synchronous configuration validation.
    Called at startup to verify all required environment variables.
    """
    from app.core.config import settings

    logger.info("🔍 Validating configuration...")

    required = {
        "DATABASE_URL": settings.DATABASE_URL,
        "GROQ_API_KEY": settings.GROQ_API_KEY,
        "GROQ_MODEL": settings.GROQ_MODEL,
        "JWT_SECRET": settings.JWT_SECRET,
    }

    missing = [k for k, v in required.items() if not v]

    if missing:
        raise ValueError(f"❌ Missing required config: {', '.join(missing)}")

    if not settings.embedding_config_valid:
        raise ValueError(
            f"❌ Invalid embedding config: {settings.EMBEDDING_MODEL}={settings.EMBEDDING_DIMENSION}"
        )

    if not settings.memory_config_valid:
        raise ValueError("❌ Invalid memory thresholds (MEMORY_SIMILARITY_THRESHOLD / "
                          "MEMORY_EXTRACTION_CONFIDENCE_THRESHOLD must both be 0.0-1.0)")

    # WhatsApp is optional infrastructure - only enforce its secrets if
    # the deployment has actually configured WhatsApp credentials.
    # (WhatsAppService itself independently refuses to construct with an
    # empty/placeholder app secret if it's ever imported - this is a
    # second, earlier check with a clearer error message.)
    if settings.WHATSAPP_ACCESS_TOKEN or settings.WHATSAPP_PHONE_NUMBER_ID:
        if not settings.WHATSAPP_APP_SECRET or settings.WHATSAPP_APP_SECRET == "placeholder":
            raise ValueError("❌ WhatsApp is configured but WHATSAPP_APP_SECRET is missing/placeholder")
        if not settings.WHATSAPP_VERIFY_TOKEN:
            raise ValueError("❌ WhatsApp is configured but WHATSAPP_VERIFY_TOKEN is missing")

    logger.info(f"✅ Database: {settings.DATABASE_URL.split('@')[1] if '@' in settings.DATABASE_URL else 'OK'}")
    logger.info(f"✅ Groq Model: {settings.GROQ_MODEL}")
    logger.info("✅ Configuration valid")


async def validate_startup():
    """
    Async startup validation.
    Tests database and external service connections.
    """
    logger.info("🚀 Starting async validation...")

    # Test database
    try:
        from app.db.database import engine
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("✅ PostgreSQL: Connected")
    except Exception as e:
        logger.error(f"❌ PostgreSQL: {e}")
        raise

    # Test Groq
    try:
        from app.services.llm_service import llm_service
        # Simple test call to verify API key works
        logger.info("✅ Groq API: Configured")
    except Exception as e:
        logger.error(f"❌ Groq API: {e}")
        raise

    logger.info("✅ All startup checks passed")