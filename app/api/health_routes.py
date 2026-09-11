"""
Health check endpoints - Issue #28 Fixed

Replaces the old simple health check with proper dependency monitoring.

Provides three endpoints:
  GET /health/live     - Liveness probe (is process alive?)
  GET /health/ready    - Readiness probe (can handle traffic?)
  GET /health/detailed - Detailed dependency status
"""

import logging
from fastapi import APIRouter, HTTPException
from app.services.dependency_health import dependency_health

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/live")
async def liveness():
    """
    Liveness probe (Kubernetes)
    
    Is the process running?
    Returns HTTP 200 if alive (even if dependencies are down)
    """
    summary = dependency_health.health_summary()
    
    return {
        "status": "alive",
        "timestamp": summary["timestamp"],
    }


@router.get("/ready")
async def readiness():
    """
    Readiness probe (Kubernetes)
    
    Can this pod handle traffic?
    Returns:
      HTTP 200: Pod is ready (chat works)
      HTTP 503: Pod not ready (critical service down - don't send traffic)
    """
    summary = dependency_health.health_summary()
    
    if not summary["chat_available"]:
        # Critical failure: chat doesn't work
        logger.error(
            "❌ Pod not ready: critical dependencies down",
            extra={"dependencies": summary["dependencies"]}
        )
        raise HTTPException(
            status_code=503,
            detail="Service unavailable: critical dependencies down"
        )
    
    # Pod is ready
    return {
        "status": "ready",
        "chat_available": summary["chat_available"],
        "memory_available": summary["memory_available"],
        "reminders_available": summary["reminders_available"],
        "timestamp": summary["timestamp"],
    }


@router.get("/detailed")
async def health_detailed():
    """
    Detailed health check
    
    Returns:
      HTTP 200: All features working
      HTTP 206: Some features degraded
      HTTP 503: Chat unavailable
    """
    summary = dependency_health.health_summary()
    
    if not summary["chat_available"]:
        # Critical failure: chat doesn't work
        logger.error(
            "❌ Health check FAILED: chat unavailable",
            extra={"dependencies": summary["dependencies"]}
        )
        raise HTTPException(
            status_code=503,
            detail="Chat service unavailable"
        )
    
    if not summary["all_dependencies_up"]:
        # Partial failure: chat works but features degraded
        logger.warning(
            "⚠️  Health check DEGRADED: premium features down",
            extra={
                "memory_available": summary["memory_available"],
                "reminders_available": summary["reminders_available"],
            }
        )
        # Return 206 Partial Content (not 200)
        return {
            "status": "degraded",
            "message": "Chat available but some features degraded",
            "chat_available": summary["chat_available"],
            "memory_available": summary["memory_available"],
            "reminders_available": summary["reminders_available"],
            "dependencies": summary["dependencies"],
            "timestamp": summary["timestamp"],
        }
    
    # All good
    logger.info("✅ All systems operational")
    return {
        "status": "healthy",
        "chat_available": True,
        "memory_available": True,
        "reminders_available": True,
        "dependencies": summary["dependencies"],
        "timestamp": summary["timestamp"],
    }


@router.get("/dependencies")
async def health_dependencies():
    """
    Dependency status endpoint
    
    Shows detailed status of each dependency
    Useful for monitoring/alerting systems
    """
    summary = dependency_health.health_summary()
    
    return {
        "timestamp": summary["timestamp"],
        "summary": {
            "all_up": summary["all_dependencies_up"],
            "chat_available": summary["chat_available"],
            "memory_available": summary["memory_available"],
            "reminders_available": summary["reminders_available"],
        },
        "services": {
            "postgres": {
                "status": summary["dependencies"]["postgres"]["status"],
                "required_for": ["chat", "memory", "reminders"],
                "last_checked": summary["dependencies"]["postgres"]["last_checked"],
                "error": summary["dependencies"]["postgres"]["error"],
            },
            "groq": {
                "status": summary["dependencies"]["groq"]["status"],
                "required_for": ["chat"],
                "last_checked": summary["dependencies"]["groq"]["last_checked"],
                "error": summary["dependencies"]["groq"]["error"],
            },
            "qdrant": {
                "status": summary["dependencies"]["qdrant"]["status"],
                "required_for": ["memory"],
                "last_checked": summary["dependencies"]["qdrant"]["last_checked"],
                "error": summary["dependencies"]["qdrant"]["error"],
            },
            "email": {
                "status": summary["dependencies"]["email"]["status"],
                "required_for": ["reminders"],
                "last_checked": summary["dependencies"]["email"]["last_checked"],
                "error": summary["dependencies"]["email"]["error"],
            },
            "scheduler": {
                "status": summary["dependencies"]["scheduler"]["status"],
                "required_for": ["reminders"],
                "last_checked": summary["dependencies"]["scheduler"]["last_checked"],
                "error": summary["dependencies"]["scheduler"]["error"],
            },
        },
        "alerts": _get_alerts(summary),
    }


def _get_alerts(summary: dict) -> list:
    """Generate alerts based on current status"""
    alerts = []
    
    if not summary["chat_available"]:
        alerts.append({
            "severity": "CRITICAL",
            "message": "Chat service unavailable",
            "action": "Check PostgreSQL and Groq API immediately"
        })
    
    if not summary["memory_available"]:
        alerts.append({
            "severity": "WARNING",
            "message": "Memory feature unavailable",
            "action": "Check Qdrant service - premium feature degraded"
        })
    
    if not summary["reminders_available"]:
        alerts.append({
            "severity": "WARNING",
            "message": "Reminders feature unavailable",
            "action": "Check email service and scheduler"
        })
    
    return alerts


# Keep old endpoint for backward compatibility (optional)
@router.get("/health")
async def health():
    """
    Old endpoint for backward compatibility
    
    Redirects to /detailed for full status
    """
    summary = dependency_health.health_summary()
    
    if not summary["chat_available"]:
        raise HTTPException(status_code=503, detail="Chat service unavailable")
    
    if not summary["all_dependencies_up"]:
        return {
            "status": "degraded",
            "message": "Chat available but some features degraded",
        }
    
    return {
        "status": "ok",
        "message": "AI Companion Platform",
    }