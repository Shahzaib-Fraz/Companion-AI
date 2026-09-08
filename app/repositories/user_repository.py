"""User data access layer"""
from sqlalchemy.orm import Session
from app.models.database import User, UserProfile

class UserRepository:
    @staticmethod
    def get_by_email(db: Session, email: str):
        return db.query(User).filter(User.email == email.lower()).first()
    
    @staticmethod
    def get_by_phone(db: Session, phone: str):
        return db.query(User).filter(User.phone_number == phone).first()
    
    @staticmethod
    def get_by_id(db: Session, user_id: int):
        return db.query(User).filter(User.id == user_id).first()
    
    @staticmethod
    def create(db: Session, email: str, phone_number: str = None):
        user = User(email=email.lower(), phone_number=phone_number)
        db.add(user)
        db.commit()
        db.refresh(user)
        return user
    
    @staticmethod
    def get_or_create_profile(db: Session, user_id: int):
        profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
        if not profile:
            profile = UserProfile(
                user_id=user_id,
                display_name=None,      # ✅ Leave empty
                language=None,          # ✅ Leave empty (not "en")
                timezone=None,          # ✅ Leave empty (not "UTC")
                account_tier=None,    # Default tier is fine
                onboarding_completed=False
            )
            db.add(profile)
            db.commit()
        return profile
        
    @staticmethod
    def update_profile(db: Session, user_id: int, **kwargs):
        profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
        if profile:
            for key, value in kwargs.items():
                if hasattr(profile, key):
                    setattr(profile, key, value)
            db.commit()
            db.refresh(profile)
        return profile
