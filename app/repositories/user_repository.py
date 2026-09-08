"""User data access layer"""
from sqlalchemy.orm import Session
from app.models.database import User, UserProfile

class UserRepository:
    @staticmethod
    def _normalize_phone(phone: str) -> str:
        """Always store/query phone numbers in +E.164 form. WhatsApp's webhook
        sends numbers with no leading '+' (e.g. '923037454400'); web/manual
        entry may or may not include it. Without this, the same real-world
        number creates two different DB rows."""
        if not phone:
            return phone
        digits = phone.strip().lstrip("+")
        return f"+{digits}"

    @staticmethod
    def get_by_email(db: Session, email: str):
        return db.query(User).filter(User.email == email.lower()).first()

    @staticmethod
    def get_by_phone(db: Session, phone: str):
        normalized = UserRepository._normalize_phone(phone)
        return db.query(User).filter(User.phone_number == normalized).first()

    @staticmethod
    def get_by_id(db: Session, user_id: int):
        return db.query(User).filter(User.id == user_id).first()

    @staticmethod
    def create(db: Session, email: str, phone_number: str = None):
        user = User(email=email.lower(), phone_number=UserRepository._normalize_phone(phone_number))
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
                display_name=None,
                language=None,
                timezone=None,
                account_tier=None,
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