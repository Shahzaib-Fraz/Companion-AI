
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
import uvicorn

from app.core.config import settings
from app.core.limiter import limiter
from app.core.startup import validate_startup, validate_config_sync
from app.api import health_routes, chat_routes, auth_routes, reminder_routes, whatsapp_routes, user_routes
from app.services.whatsapp_service import whatsapp_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FIXED P0-2: Startup validation
    FIXED P0-3: Do NOT start scheduler here

    Scheduler runs in a separate process:
        python scheduler_worker.py
    """
    logger.info("=" * 70)
    logger.info("🚀 Application Startup")
    logger.info("=" * 70)

    # Step 1: Synchronous config validation
    try:
        validate_config_sync()
    except Exception as e:
        logger.error(f"❌ Config validation failed: {e}")
        raise

    # Step 2: Async dependency validation (P0-2)
    try:
        await validate_startup()
    except Exception as e:
        logger.error(f"❌ Startup validation failed: {e}")
        logger.error("❌ STARTUP FAILED - Application cannot start without critical dependencies")
        raise

    # Step 3: Ready to serve
    logger.info("✅ Application ready on port 8000")
    logger.info("📌 Note: Scheduler runs in separate process (python scheduler_worker.py)")
    logger.info("=" * 70)

    yield

    # ========================
    # Shutdown
    # ========================
    logger.info("⏹️  Shutting down...")
    logger.info("👋 Application shutdown complete")


# Create FastAPI app
app = FastAPI(
    title="AI Companion API",
    description="AI-powered chat companion with reminders",
    version="1.0.0",
    lifespan=lifespan,
)

# Rate limiting - shared instance, see app/core/limiter.py
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ========================================================================
# MIDDLEWARE
# ========================================================================

app.state.limiter_enabled = True

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Global rate limiting middleware"""
    try:
        # Skip health endpoints
        if request.url.path.startswith("/health"):
            return await call_next(request)

        # Apply global limit if limiter is enabled
        if getattr(app.state, "limiter_enabled", True):
            await app.state.limiter.hit(request)
    except Exception:
        # If limiter fails, continue (don't block traffic)
        pass

    return await call_next(request)


# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ========================================================================
# ROUTES
# ========================================================================

app.include_router(health_routes.router, prefix="/health", tags=["health"])
app.include_router(auth_routes.router, prefix="/auth", tags=["auth"])
app.include_router(chat_routes.router, prefix="/chat", tags=["chat"])
app.include_router(reminder_routes.router, prefix="/reminders", tags=["reminders"])
app.include_router(whatsapp_routes.router, prefix="/webhook", tags=["webhooks"])
app.include_router(user_routes.router, prefix="/users", tags=["users"])  # was missing entirely


# ========================================================================
# EXCEPTION HANDLERS
# ========================================================================

@app.exception_handler(404)
async def not_found(request: Request, exc):
    return JSONResponse(
        status_code=404,
        content={"detail": f"Not found: {request.url.path}"}
    )


@app.exception_handler(500)
async def server_error(request: Request, exc):
    logger.error(f"❌ Unhandled error: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"}
    )


# ========================================================================
# ROOT ENDPOINT
# ========================================================================

@app.get("/", tags=["root"])
async def root():
    return {
        "name": "AI Companion API",
        "version": "1.0.0",
        "status": "running",
        "docs": "/docs",
        "health": "/health/live",
        "ready": "/health/ready",
    }


# ========================================================================
# DEPLOYMENT NOTES
# ========================================================================

"""
IMPORTANT: Deployment Setup

The scheduler is a SEPARATE PROCESS and must NOT run in gunicorn workers.
Run both from the REPO ROOT (this file lives at app/main.py):

BEFORE (broken - causes duplicates):
    gunicorn -w 4 main:app

AFTER (correct):
    Process 1: gunicorn -w 4 -b 0.0.0.0:8000 app.main:app   # API only
    Process 2: python scheduler_worker.py                   # Separate process for reminders

Use Supervisor, Procfile, or docker-compose to manage both processes:

With Supervisor (/etc/supervisor/conf.d/ai-companion.conf):
    [program:api]
    command=gunicorn -w 4 -b 0.0.0.0:8000 app.main:app
    directory=/path/to/repo
    autostart=true
    autorestart=true

    [program:scheduler]
    command=python scheduler_worker.py
    directory=/path/to/repo
    autostart=true
    autorestart=true
    numprocs=1

With docker-compose:
    services:
      api:
        command: gunicorn -w 4 -b 0.0.0.0:8000 app.main:app
      scheduler:
        command: python scheduler_worker.py
"""

if __name__ == "__main__":
    # Development only - use gunicorn for production
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
        workers=1,  # Single worker in dev
    )