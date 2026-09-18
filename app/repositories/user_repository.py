"""
User Repository - Data access for users
File location: app/repositories/user_repository.py

Stub implementation - extend with your actual methods.
"""
import logging
import re
from sqlalchemy.orm import Session
from app.models.database import User, UserProfile

logger = logging.getLogger(__name__)


class UserRepository:
    """Repository for user operations"""
    
    @staticmethod
    def _normalize_phone(phone: str) -> str:
        """
        Normalize phone number to +CC format.
        
        Examples:
            923037454400 → +923037454400
            +923037454400 → +923037454400
            03037454400 → +923037454400
        """
        if not phone:
            return None
        
        # Remove all non-digits and +
        cleaned = re.sub(r'[^\d+]', '', phone)
        
        # Remove leading zeros if no +
        if not cleaned.startswith('+'):
            cleaned = cleaned.lstrip('0')
        
        # Add + if not present
        if not cleaned.startswith('+'):
            # Assume Pakistan country code (92)
            if cleaned.startswith('0'):
                cleaned = '92' + cleaned[1:]
            else:
                cleaned = '92' + cleaned
            cleaned = '+' + cleaned
        
        return cleaned
    
    @staticmethod
    def get_by_id(db: Session, user_id: int) -> User:
        """Get user by ID"""
        try:
            return db.query(User).filter(User.id == user_id).first()
        except Exception as e:
            logger.error(f"Failed to get user by ID: {e}")
            return None
    
    @staticmethod
    def get_by_email(db: Session, email: str) -> User:
        """Get user by email"""
        try:
            return db.query(User).filter(User.email == email.lower()).first()
        except Exception as e:
            logger.error(f"Failed to get user by email: {e}")
            return None
    
    @staticmethod
    def get_by_phone(db: Session, phone: str) -> User:
        """Get user by phone number with normalization"""
        try:
            normalized = UserRepository._normalize_phone(phone)
            if not normalized:
                return None
            
            return db.query(User).filter(User.phone_number == normalized).first()
        except Exception as e:
            logger.error(f"Failed to get user by phone: {e}")
            return None
    
    @staticmethod
    def get_or_create_profile(db: Session, user_id: int) -> UserProfile:
        """Get or create user profile"""
        try:
            profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
            
            if not profile:
                profile = UserProfile(user_id=user_id)
                db.add(profile)
                db.flush()
            
            return profile
        except Exception as e:
            logger.error(f"Failed to get/create profile: {e}")
            return None
    
    @staticmethod
    def create(db: Session, email: str, password_hash: str, phone: str = None) -> User:
        """Create a new user"""
        try:
            user = User(
                email=email.lower(),
                password_hash=password_hash,
                phone_number=UserRepository._normalize_phone(phone) if phone else None,
            )
            db.add(user)
            db.flush()
            return user
        except Exception as e:
            logger.error(f"Failed to create user: {e}")
            return None
    
    @staticmethod
    def update_phone(db: Session, user_id: int, phone: str) -> bool:
        """Update user phone number"""
        try:
            user = UserRepository.get_by_id(db, user_id)
            if user:
                user.phone_number = UserRepository._normalize_phone(phone)
                db.flush()
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to update phone: {e}")
            return False