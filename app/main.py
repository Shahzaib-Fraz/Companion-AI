"""AI Companion Platform - FastAPI Backend
Pure chat-based authentication with unified Web + WhatsApp support

FIXED: Event loop closed error using nest_asyncio
FIXED: Issue #19 - Rate limiting added to prevent DDoS
FIXED: Issue #27 - Lazy load embedding model (no warmup on startup)
FIXED: Issue #28 - Track dependency health status
FIXED: Added plain GET /health — health_routes.router only exposes
       /health/live, /health/ready, /health/detailed, /health/dependencies,
       and /health/health (backward-compat route + prefix collision).
       Bare /health was never actually registered, so any client (e.g.
       Streamlit) hitting /health directly got a 404 even though the
       backend was fully up.
"""

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime

import nest_asyncio  # ← FIX: Allow nested asyncio.run() calls
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter  # ✅ Issue #19: Rate limiting
from slowapi.util import get_remote_address  # ✅ Issue #19
from slowapi.errors import RateLimitExceeded  # ✅ Issue #19
from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import settings, validate_settings_on_startup

from app.api import (
    auth_routes,
    chat_routes,
    health_routes,
    reminder_routes,
    user_routes,
    whatsapp_routes,
)
from app.core.config import settings
from app.db.database import Base, engine, get_db
from app.services.embedding_service import embedding_service
from app.services.scheduler_service import get_reminder_scheduler_service
from app.services.dependency_health import dependency_health  # ✅ FIXED #28

# Create all tables
Base.metadata.create_all(bind=engine)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# FIX: Apply nest_asyncio to handle repeated asyncio.run() calls in scheduler
# This allows asyncio.run() to be called multiple times without "Event loop is closed" error
nest_asyncio.apply()

# Global scheduler instance
scheduler = BackgroundScheduler()


def _quiet_noisy_loggers():
    """
    Third-party INFO chatter that drowns out this app's own log lines.

    httpx logs one line per outbound request, so every Groq call, every Qdrant
    call and ~20 Hugging Face cache-validation HEADs each get a line. The
    apscheduler executor logs "Running job / executed successfully" every 60
    seconds forever. None of it is actionable, and it makes real errors hard to
    find when you are reading a log to debug.

    Job FAILURES are unaffected: scheduler_service registers an EVENT_JOB_ERROR
    listener that logs through its own logger, so crashes still surface.
    """
    for name in (
        "httpx",
        "httpcore",
        "huggingface_hub.utils._http",
        "sentence_transformers.base.model",
        "apscheduler.executors.default",
        "apscheduler.scheduler",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    # Startup

    try:
        validate_settings_on_startup()
        logger.info("✅ Configuration validated")
    except ValueError as e:
        logger.critical(f"❌ Config validation failed: {e}")
        raise SystemExit(1)
    
    logger.info("=" * 60)
    logger.info("🚀 AI Companion Platform Starting...")
    logger.info("=" * 60)
    logger.info(f"⏰ Startup time: {datetime.now().isoformat()}")
    
    # ✅ FIXED #28: Initialize and check dependencies
    try:
        from app.db.database import engine as db_engine
        from app.models.base import Base as BaseModel
        BaseModel.metadata.create_all(bind=db_engine)
        dependency_health.set_status("postgres", "up")
        logger.info("✅ PostgreSQL initialized")
    except Exception as e:
        dependency_health.set_status("postgres", "down", str(e))
        logger.error(f"❌ PostgreSQL initialization failed: {e}")
    
    logger.info("🔐 Authentication: JWT + Phone-based")
    logger.info("💾 Memory: PostgreSQL + Qdrant (Premium)")
    logger.info("📧 Email: Brevo (formerly Sendinblue)")
    logger.info(f"🤖 LLM: Groq ({settings.GROQ_MODEL})")

    from app.services.whatsapp_service import whatsapp_service
    logger.info("WhatsApp service initialized")

    # FIXED (Issue #13): Start scheduler with async-compatible job
    try:
        if not scheduler.running:
            # Get database session for scheduler
            db = get_db().__next__()
            reminder_service = get_reminder_scheduler_service(db)
            
            # FIXED: Wrap async function for APScheduler
            # asyncio.run() is now safe to use repeatedly thanks to nest_asyncio
            scheduler.add_job(
                lambda: asyncio.run(reminder_service.send_reminders()),
                trigger="interval",
                minutes=1,  # Run every minute for testing, change to 5 for production
                id="reminder_dispatcher",
                name="Reminder Dispatcher",
                replace_existing=True
            )
            
            scheduler.start()
            dependency_health.set_status("scheduler", "up")  # ✅ FIXED #28
            logger.info("✅ APScheduler started (reminders every 1 minute)")
            logger.info("   Job: reminder_dispatcher (async-aware wrapper with nest_asyncio)")
        else:
            logger.info("✅ APScheduler already running")
            dependency_health.set_status("scheduler", "up")  # ✅ FIXED #28
    except Exception as e:
        dependency_health.set_status("scheduler", "down", str(e))  # ✅ FIXED #28
        logger.error(f"❌ Failed to start scheduler: {e}", exc_info=True)

    # Set AFTER scheduler start, which raises the apscheduler logger to INFO.
    # Otherwise that call would undo this.
    _quiet_noisy_loggers()

    # ✅ FIXED Issue #27: Embedding model loads lazily (no warmup on startup)
    # Model loads on first embed() call, not at startup.
    # This saves 60+ seconds on startup (4 workers) and 75% RAM.
    logger.info("ℹ️  Embedding model will load on first use (lazy loading)")
    logger.info("   First request may take 5-10s, subsequent requests instant")

    yield

    # Shutdown
    logger.info("🛑 Shutting down...")
    
    # ✅ FIXED #28: Mark dependencies as down on shutdown
    dependency_health.set_status("postgres", "down", "Shutdown")
    dependency_health.set_status("scheduler", "down", "Shutdown")
    dependency_health.set_status("qdrant", "down", "Shutdown")
    
    try:
        if scheduler.running:
            scheduler.shutdown()
            logger.info("✅ APScheduler stopped")
    except Exception as e:
        logger.error(f"❌ Error stopping scheduler: {e}")
    logger.info("👋 Goodbye!")


# Create FastAPI app
app = FastAPI(
    title="AI Companion Platform",
    description="Chat-based AI companion with unified Web & WhatsApp context",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ✅ Issue #19: Initialize rate limiter
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter


# ✅ Issue #19: Exception handler for rate limit exceeded
@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request, exc):
    """
    Handle rate limit exceeded errors.
    
    Returns 429 with informative message when user exceeds rate limit.
    Logs the rate limit violation for monitoring.
    """
    logger.warning(
        "⏳ Rate limit exceeded",
        extra={
            "path": request.url.path,
            "ip": get_remote_address(request),
            "method": request.method,
        }
    )
    return {
        "error": "Rate limit exceeded",
        "message": "Too many requests. Please try again later.",
        "retry_after": "60 seconds",
        "status_code": 429,
    }


# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================================================================ ROUTES

# ✅ FIXED #28: Include health routes with proper prefix
# NOTE: this exposes /health/live, /health/ready, /health/detailed,
# /health/dependencies, and /health/health — but NOT bare /health.
# See the plain /health route below for that.
app.include_router(health_routes.router, prefix="/health", tags=["health"])

# Include other routers
app.include_router(auth_routes.router, prefix="/auth", tags=["authentication"])
app.include_router(chat_routes.router, prefix="/chat", tags=["chat"])
app.include_router(user_routes.router, prefix="/users", tags=["users"])
app.include_router(reminder_routes.router, prefix="/reminders", tags=["reminders"])
app.include_router(whatsapp_routes.router, tags=["whatsapp"])  # WhatsApp webhook


# Root endpoint
@app.get("/")
def root():
    """Root endpoint"""
    return {
        "message": "🤖 AI Companion Platform API",
        "status": "running",
        "docs": "/docs",
        "version": "1.0.0",
    }


# ✅ FIX: Plain /health for simple clients (e.g. Streamlit) that just want
# a fast "is the backend reachable" check. This is intentionally separate
# from health_routes.router — that router's routes live under /health/*
# (live, ready, detailed, dependencies) and never registered bare /health.
@app.get("/health")
def simple_health():
    """Lightweight liveness check for frontend clients."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )