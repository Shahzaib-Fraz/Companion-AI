
import logging
import os
from datetime import datetime

import httpx
from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/")
async def health():
    """
    Basic health check.
    Returns status without checking dependencies.
    """
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "scheduler_running": False
    }


@router.get("/ready")
async def ready():
    """
    Readiness check - verifies critical dependencies with live probes.
    Returns 503 if any dependency is down.
    """
    dependencies = {
        "postgres": False,
        "qdrant": False,
        "groq": False,
        "email": False,
    }

    # Check PostgreSQL
    try:
        from app.db.database import engine
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        dependencies["postgres"] = True
        logger.info("✅ PostgreSQL: Connected")
    except Exception as e:
        logger.error(f"❌ PostgreSQL: {e}")
        dependencies["postgres"] = False

    # Check Qdrant
    try:
        qdrant_url = os.getenv("QDRANT_URL", "https://localhost:6333")
        qdrant_key = os.getenv("QDRANT_API_KEY")

        headers = {}
        if qdrant_key:
            headers["api-key"] = qdrant_key

        response = httpx.get(
            f"{qdrant_url}/health",
            headers=headers,
            timeout=10,
        )

        if response.status_code in (200, 403):  # 403 means auth issue but service is up
            dependencies["qdrant"] = True
            logger.info(f"✅ Qdrant: {response.status_code}")
        else:
            logger.error(f"❌ Qdrant: {response.status_code}")
            dependencies["qdrant"] = False
    except Exception as e:
        logger.error(f"❌ Qdrant: {e}")
        dependencies["qdrant"] = False

    # Check Groq API - a LIVE probe, not just "did the client construct".
    try:
        groq_key = os.getenv("GROQ_API_KEY")
        if not groq_key:
            logger.error("❌ Groq: No API key configured")
            dependencies["groq"] = False
        else:
            from groq import Groq
            client = Groq(api_key=groq_key)
            client.models.list()  # cheap real round-trip - catches a bad/expired key
            dependencies["groq"] = True
            logger.info("✅ Groq: Live-checked")
    except Exception as e:
        logger.error(f"❌ Groq: {e}")
        dependencies["groq"] = False

    # Check Email (Brevo) - configuration-presence check only, NOT a live
    # send or API call. A live probe would cost provider quota on every
    # readiness poll (Kubernetes typically polls this every few seconds),
    # so this deliberately only verifies the pieces needed to send are
    # configured. It will not catch a revoked/expired key the way the
    # Groq check above does - that's a known, accepted gap, not an
    # oversight.
    try:
        brevo_key = os.getenv("BREVO_API_KEY")
        brevo_sender = os.getenv("BREVO_SENDER_EMAIL")
        dependencies["email"] = bool(brevo_key and brevo_sender)
        if dependencies["email"]:
            logger.info("✅ Email: Configured")
        else:
            logger.error("❌ Email: BREVO_API_KEY or BREVO_SENDER_EMAIL not configured")
    except Exception as e:
        logger.error(f"❌ Email: {e}")
        dependencies["email"] = False

    all_ready = all(dependencies.values())

    if all_ready:
        logger.info("✅ Pod ready: all dependencies up")
        return {
            "status": "ready",
            "timestamp": datetime.utcnow().isoformat(),
            **dependencies
        }
    else:
        logger.error(f"❌ Pod not ready: {dependencies}")
        from fastapi import HTTPException
        raise HTTPException(
            status_code=503,
            detail=f"Service unavailable. Dependencies: {dependencies}"
        )