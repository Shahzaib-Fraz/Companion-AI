"""Reminder API endpoints - Create, read, update reminders"""
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Header
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.services.reminder_service import reminder_service
from app.core.security import verify_access_token

logger = logging.getLogger(__name__)

router = APIRouter()


class CreateReminderRequest(BaseModel):
    """Request model for creating a reminder"""
    content: str = Field(..., min_length=2, max_length=500)
    scheduled_at_iso: str = Field(
        ...,
        description="ISO 8601 datetime with timezone: '2026-09-15T14:30:00+05:00'",
    )
    user_timezone: str = Field(default="UTC")


class ReminderResponse(BaseModel):
    """Response model for reminder operations"""
    status: str
    reminder_id: Optional[int] = None
    content: str
    scheduled_at_local: str


@router.post("/create", response_model=ReminderResponse)
async def create_reminder(
    req: CreateReminderRequest,
    db: Session = Depends(get_db),
    authorization: str = Header(None),
):
    """
    Create a reminder via API.
    
    Requires JWT token in Authorization header: Bearer <token>
    
    FIXED: Now delegates to reminder_service.maybe_create_from_scheduled_at()
    This ensures timezone handling is consistent with chat reminders.
    Fixes issue #4 (inconsistent timezone handling).
    
    Args:
        req: CreateReminderRequest containing content, scheduled_at_iso, user_timezone
        db: Database session
        authorization: JWT token in Authorization header
        
    Returns:
        ReminderResponse with created reminder details
        
    Raises:
        HTTPException: If token is missing, invalid, or reminder creation fails
    """
    
    # ========== AUTHENTICATION ==========
    # Extract and verify JWT token from Authorization header
    if not authorization or not authorization.startswith("Bearer "):
        logger.warning("Missing or malformed authorization header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authorization token. Use: Authorization: Bearer <token>",
        )
    
    # Extract token (remove "Bearer " prefix)
    token = authorization.replace("Bearer ", "")
    
    # Verify token and get user_id
    user_id = verify_access_token(token)
    if user_id is None:
        logger.warning("Invalid or expired token provided")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    
    # ========== VALIDATION ==========
    try:
        # Parse ISO 8601 datetime with timezone
        scheduled_at = datetime.fromisoformat(req.scheduled_at_iso)
        
        # Ensure timezone info is present
        if scheduled_at.tzinfo is None:
            raise ValueError(
                f"scheduled_at_iso must include timezone (got {req.scheduled_at_iso!r})"
            )
        
    except ValueError as exc:
        logger.warning("Invalid datetime from user %d: %s", user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid scheduled_at_iso: {exc}",
        )
    
    # ========== REMINDER CREATION ==========
    try:
        # Delegate to reminder_service for consistent timezone handling
        result = await reminder_service.maybe_create_from_scheduled_at(
            db=db,
            user_id=user_id,
            content=req.content,
            scheduled_at_local=scheduled_at,
            user_timezone=req.user_timezone,
        )
        
        # Validate result
        if not result:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Could not create reminder",
            )
        
        # Check for errors from service
        if result.get("status") == "error":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=result.get("error", "Unknown error"),
            )
        
        # Verify success status
        if result.get("status") != "created":
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Unexpected response from service",
            )
        
        # Extract reminder from result
        reminder = result["reminder"]
        
        logger.info(
            "Reminder created for user %d: id=%d, content='%s', scheduled_at=%s",
            user_id,
            reminder.id,
            reminder.content,
            reminder.scheduled_at,
        )
        
        return ReminderResponse(
            status="created",
            reminder_id=reminder.id,
            content=reminder.content,
            scheduled_at_local=result["local_time"],
        )
        
    except ValueError as exc:
        logger.warning("Reminder creation validation failed for user %d: %s", user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except Exception as exc:
        logger.exception("Unexpected error creating reminder for user %d: %s", user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )