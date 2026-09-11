"""User data access layer"""
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.models.database import User, UserProfile
from app.core.security import hash_password

class UserRepository:
    @staticmethod
    def _normalize_phone(phone: str) -> str:
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
    def create(db: Session, email: str, phone_number: str = None, password: str = None):
        user = User(
            email=email.lower(),
            phone_number=UserRepository._normalize_phone(phone_number),
            password_hash=hash_password(password) if password else None,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user
    
    @staticmethod
    def get_or_create_profile(db: Session, user_id: int):
        # Query first — avoids a failed INSERT (and rollback) on every
        # call for users who already have a profile, which after
        # onboarding is every single chat turn.
        profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
        if profile:
            return profile

        try:
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
            db.refresh(profile)
            return profile
        except IntegrityError:
            # Race: another request created it between our SELECT and INSERT
            db.rollback()
            profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
            if not profile:
                raise
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

    @staticmethod
    def merge_into(db: Session, from_user_id: int, to_user_id: int) -> None:
        """
        Move a WhatsApp-first stub account's identity onto an existing real
        (password-holding) account, then delete the stub.

        Used when a WhatsApp-first user's onboarding email turns out to
        already belong to a real web account — without this, WhatsApp and
        web permanently split into two disconnected identities for the same
        person (review item #5).
        """
        from app.models.database import Message, Conversation, Reminder

        from_user = db.query(User).filter(User.id == from_user_id).first()
        to_user = db.query(User).filter(User.id == to_user_id).first()
        if not from_user or not to_user:
            return

        if from_user.phone_number and not to_user.phone_number:
            to_user.phone_number = from_user.phone_number

        db.query(Message).filter(Message.user_id == from_user_id).update({"user_id": to_user_id})
        db.query(Conversation).filter(Conversation.user_id == from_user_id).update({"user_id": to_user_id})
        db.query(Reminder).filter(Reminder.user_id == from_user_id).update({"user_id": to_user_id})

        db.query(UserProfile).filter(UserProfile.user_id == from_user_id).delete()
        db.delete(from_user)
        db.commit()