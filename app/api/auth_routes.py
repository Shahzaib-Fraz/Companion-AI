import logging
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.database import User, UserProfile
from app.core.security import (
    hash_password,
    verify_password,
    create_access_token,
    verify_access_token,
)
from app.core.limiter import limiter
from app.schemas.schemas import PIIMasker

logger = logging.getLogger(__name__)
router = APIRouter()


class SignupRequest(BaseModel):
    """Signup request"""
    email: EmailStr = Field(..., description="User email")
    password: str = Field(..., min_length=8, max_length=128, description="Password (8+ chars)")


class LoginRequest(BaseModel):
    """Login request"""
    email: EmailStr = Field(..., description="User email")
    password: str = Field(..., description="User password")


class AuthResponse(BaseModel):
    """Authentication response"""
    access_token: str
    token_type: str = "bearer"
    user_id: int
    email: str


# P1-17 FIX: signup/login had NO rate limiting at all before - only chat
# was throttled. Brute-force login and signup-spam were both wide open.
@router.post("/signup", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
def signup(request: Request, payload: SignupRequest, db: Session = Depends(get_db)):
    """
    Create a new user account.

    Returns:
        AuthResponse with access_token and user_id

    Raises:
        HTTPException(409): Email already registered
        HTTPException(400): Invalid email format
    """

    # Step 1: Validate input (Pydantic handles EmailStr validation)
    logger.info(f"Signup attempt for email: {PIIMasker.mask_email(payload.email)}")

    # Step 2: Check if user already exists
    existing = db.query(User).filter(User.email == payload.email.lower()).first()
    if existing:
        logger.warning(f"❌ Signup: Email already registered: {PIIMasker.mask_email(payload.email)}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists"
        )

    # Step 3: Hash password
    try:
        password_hash = hash_password(payload.password)
    except Exception as e:
        logger.error(f"❌ Password hashing failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )

    # Step 4: Create user
    try:
        user = User(
            email=payload.email.lower(),
            password_hash=password_hash,
            is_active=True,
        )
        db.add(user)
        db.flush()  # Flush to get user.id without commit

        # Step 5: Create user profile
        profile = UserProfile(
            user_id=user.id,
            display_name=None,
            timezone="UTC",
            language="en",
            onboarding_completed=False,
            onboarding_step=0,
        )
        db.add(profile)
        db.commit()
        db.refresh(user)

        logger.info(f"✅ Signup successful: user_id={user.id}, email={PIIMasker.mask_email(user.email)}")

    except Exception as e:
        db.rollback()
        logger.error(f"❌ Failed to create user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create account"
        )

    # Step 6: Generate JWT token
    try:
        access_token = create_access_token(user.id)
    except Exception as e:
        logger.error(f"❌ Token generation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate token"
        )

    return AuthResponse(
        access_token=access_token,
        token_type="bearer",
        user_id=user.id,
        email=user.email,
    )


@router.post("/login", response_model=AuthResponse)
@limiter.limit("10/minute")
def login(request: Request, payload: LoginRequest, db: Session = Depends(get_db)):
    """
    Authenticate existing user.

    Returns:
        AuthResponse with access_token and user_id

    Raises:
        HTTPException(401): Invalid email or password
    """

    logger.info(f"Login attempt for email: {PIIMasker.mask_email(payload.email)}")

    # Step 1: Find user by email
    user = db.query(User).filter(User.email == payload.email.lower()).first()

    if not user:
        logger.warning(f"❌ Login: User not found: {PIIMasker.mask_email(payload.email)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password"
        )

    # Step 2: Verify password
    if not user.password_hash or not verify_password(payload.password, user.password_hash):
        logger.warning(f"❌ Login: Invalid password for user {user.id}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password"
        )

    # Step 3: Check if user is active
    if not user.is_active:
        logger.warning(f"❌ Login: Inactive user {user.id}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is disabled"
        )

    # Step 4: Generate JWT token
    try:
        access_token = create_access_token(user.id)
    except Exception as e:
        logger.error(f"❌ Token generation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate token"
        )

    logger.info(f"✅ Login successful: user_id={user.id}")

    return AuthResponse(
        access_token=access_token,
        token_type="bearer",
        user_id=user.id,
        email=user.email,
    )


@router.get("/verify", tags=["auth"])
def verify_token(authorization: str = Header(None)):
    """
    Verify JWT token validity.

    BUG FIX: this used to be `authorization: str = None` with no
    Header(...) marker. FastAPI treats an unannotated str parameter as
    a QUERY parameter by default, not the Authorization HTTP header -
    unlike chat_routes.py, user_routes.py, and reminder_routes.py, which
    all correctly use Header(None) for the same thing. This endpoint has
    never actually read the Authorization header a real client sends;
    `authorization` was always None here regardless of what was sent,
    so this endpoint always fell into the "invalid" branch below and
    reported every token as invalid. The same bug is fixed in
    refresh_token() below - both were broken the same way.

    Args:
        authorization: Optional Authorization header (Bearer <token>)

    Returns:
        {valid: bool, user_id: int|null}
    """

    # No header = invalid
    if not authorization or not authorization.startswith("Bearer "):
        logger.debug("Token verification: Missing or invalid header")
        return {
            "valid": False,
            "user_id": None,
        }

    # Extract and verify token
    token = authorization.replace("Bearer ", "")
    user_id = verify_access_token(token)

    if user_id is None:
        logger.debug("Token verification: Invalid or expired token")
        return {
            "valid": False,
            "user_id": None,
        }

    logger.debug(f"Token verification: Valid for user {user_id}")
    return {
        "valid": True,
        "user_id": user_id,
    }


@router.post("/refresh", response_model=AuthResponse, tags=["auth"])
def refresh_token(authorization: str = Header(None), db: Session = Depends(get_db)):
    """
    Refresh an expired access token.

    Args:
        authorization: Current token in Authorization header
        db: Database session

    Returns:
        AuthResponse with new access_token

    Raises:
        HTTPException(401): Invalid or missing token
    """

    # Validate header
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authorization header"
        )

    # Verify token
    token = authorization.replace("Bearer ", "")
    user_id = verify_access_token(token)

    if user_id is None:
        logger.warning("Token refresh: Invalid or expired token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token"
        )

    # Get user
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )

    # Generate new token
    try:
        access_token = create_access_token(user_id)
    except Exception as e:
        logger.error(f"Token refresh: Generation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate token"
        )

    logger.info(f"✅ Token refreshed for user {user_id}")

    return AuthResponse(
        access_token=access_token,
        token_type="bearer",
        user_id=user.id,
        email=user.email,
    )