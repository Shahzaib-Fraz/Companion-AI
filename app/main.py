"""AI Companion Platform - FastAPI Backend
Pure chat-based authentication with unified Web + WhatsApp support
"""

import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
from app.services.scheduler_service import scheduler_service

# Create all tables
Base.metadata.create_all(bind=engine)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


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
    logger.info("=" * 60)
    logger.info("🚀 AI Companion Platform Starting...")
    logger.info("=" * 60)
    logger.info(f"⏰ Startup time: {datetime.now().isoformat()}")
    logger.info("📊 Database: PostgreSQL (auto-initialized)")
    logger.info("🔐 Authentication: JWT + Phone-based")
    logger.info("💾 Memory: PostgreSQL + Qdrant (Premium)")
    logger.info("📧 Email: Mailtrap (Free tier)")
    logger.info(f"🤖 LLM: Groq ({settings.GROQ_MODEL})")

    # Start scheduler for reminders
    try:
        scheduler_service.start()
        logger.info("✅ APScheduler started (reminders every 1 minute)")
    except Exception as e:
        logger.error(f"❌ Failed to start scheduler: {e}")

    # Set AFTER scheduler_service.start(), which raises the apscheduler logger
    # to INFO. Otherwise that call would undo this.
    _quiet_noisy_loggers()

    # Warm the embedding model in the BACKGROUND.
    #
    # `import sentence_transformers` pulls in torch (~25s cold on a laptop CPU)
    # and SentenceTransformer(...) then loads weights and re-validates the HF
    # cache (~10s more). That import lives inside embedding_service._load(), so
    # without this the entire 35s lands on the first premium user's first
    # message - which is exactly why a chat turn took 40 seconds and the
    # frontend timed out at 30.
    #
    # A daemon thread, not a direct call: the server binds its port and serves
    # requests immediately while the model loads alongside. Free-tier users are
    # never affected either way; only premium chat turns embed anything.
    threading.Thread(
        target=embedding_service.warmup,
        name="embedding-warmup",
        daemon=True,
    ).start()
    logger.info("⏳ Embedding model warming up in background")

    yield

    # Shutdown
    logger.info("🛑 Shutting down...")
    try:
        scheduler_service.stop()
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

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(health_routes.router, tags=["health"])
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )