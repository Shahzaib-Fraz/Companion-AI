"""User API endpoints - profile management"""
import logging
from fastapi import APIRouter, Depends, HTTPException, status, Header
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.repositories.user_repository import UserRepository
from app.core.security import verify_access_token

logger = logging.getLogger(__name__)
router = APIRouter()


def get_user_id_from_header(authorization: str = Header(None)) -> int:
    """
    Extract and validate user_id from Authorization header.
    
    Distinguishes between:
    1. No Authorization header → Returns None (optional auth)
    2. Authorization header present but invalid/expired → Raises 401
    
    Args:
        authorization: Authorization header value
        
    Returns:
        user_id if valid, None if no header
        
    Raises:
        HTTPException(401): If header present but invalid/expired
    """
    # Case 1: No authorization header - optional auth allowed
    if not authorization:
        return None
    
    # Case 2: Authorization header present - must be valid
    if not authorization.startswith("Bearer "):
        logger.warning("Invalid authorization header format")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format. Use: Authorization: Bearer <token>"
        )
    
    # Extract token and verify
    token = authorization.replace("Bearer ", "")
    user_id = verify_access_token(token)
    
    if user_id is None:
        logger.warning("Invalid or expired token provided")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token"
        )
    
    return user_id


@router.get("/profile")
async def get_profile(
    db: Session = Depends(get_db),
    user_id: int = Depends(get_user_id_from_header),
):
    """
    Get user profile.
    
    Optional authentication - works with or without token.
    If token provided and invalid, returns 401.
    
    Args:
        db: Database session
        user_id: User ID from token (None if not authenticated)
        
    Returns:
        User profile with display_name, language, timezone, account_tier
        
    Raises:
        HTTPException(401): If auth header present but invalid/expired
    """
    
    # If not authenticated, return limited profile or ask to login
    if not user_id:
        return {
            "status": "unauthenticated",
            "message": "Please sign in to view your profile",
            "profile": None
        }
    
    try:
        profile = UserRepository.get_or_create_profile(db, user_id)
        
        if not profile:
            logger.warning("Profile not found for user %d", user_id)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Profile not found"
            )
        
        logger.info("Profile retrieved for user %d", user_id)
        
        return {
            "status": "success",
            "profile": {
                "display_name": profile.display_name,
                "language": profile.language,
                "timezone": profile.timezone,
                "account_tier": profile.account_tier
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error retrieving profile for user %d: %s", user_id, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error retrieving profile"
        )


@router.put("/profile")
async def update_profile(
    update_data: dict,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_user_id_from_header),
):
    """
    Update user profile.
    
    Requires authentication (401 if token missing or invalid).
    
    Args:
        update_data: Fields to update
        db: Database session
        user_id: User ID from token
        
    Returns:
        Updated profile
        
    Raises:
        HTTPException(401): If not authenticated
        HTTPException(400): If invalid data
    """
    
    # Require authentication for updates
    if not user_id:
        logger.warning("Unauthenticated profile update attempt")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required to update profile"
        )
    
    try:
        # Validate update data
        if not update_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No fields to update"
            )
        
        # Only allow specific fields to be updated
        allowed_fields = {"display_name", "language", "timezone", "response_style"}
        invalid_fields = set(update_data.keys()) - allowed_fields
        
        if invalid_fields:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot update fields: {', '.join(invalid_fields)}"
            )
        
        profile = UserRepository.get_or_create_profile(db, user_id)
        
        # Update allowed fields
        for field, value in update_data.items():
            if field in allowed_fields and value is not None:
                setattr(profile, field, value)
        
        db.commit()
        db.refresh(profile)
        
        logger.info("Profile updated for user %d", user_id)
        
        return {
            "status": "success",
            "profile": {
                "display_name": profile.display_name,
                "language": profile.language,
                "timezone": profile.timezone,
                "account_tier": profile.account_tier
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error updating profile for user %d: %s", user_id, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error updating profile"
        )